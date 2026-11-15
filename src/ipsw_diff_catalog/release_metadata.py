from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from ipsw_diff_catalog.git import (
    blob_at_path,
    ensure_repository,
    require_origin,
    resolve_commit,
    tree_paths,
)
from ipsw_diff_catalog.model import CatalogError, JsonObject, canonical_json, parse_json_object
from ipsw_diff_catalog.pallas import display_version, read_pallas_entries
from ipsw_diff_catalog.release_source import appledb_platform
from ipsw_diff_catalog.render import load_entries

_APPLEDB_REPOSITORY = "https://github.com/littlebyteorg/appledb"
_BUILD = re.compile(r"[A-Za-z0-9]+")
_BASE_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_BETA_QUALIFIER = re.compile(r"beta(?: [1-9][0-9]*)?")
_RC_QUALIFIER = re.compile(r"RC(?: [1-9][0-9]*)?")
_PLATFORM_ORDER = {"iOS": 0, "macOS": 1}


@dataclass(frozen=True)
class ReleaseMetadataResult:
    output: Path
    release_count: int
    beta_count: int
    rc_count: int
    checked: bool


@dataclass(frozen=True)
class _RequiredRelease:
    catalog_platform: str
    build: str
    version: str
    appledb_platform: str


@dataclass(frozen=True)
class ReleaseRecord:
    """One endpoint's label, from AppleDB or, until AppleDB records it, from Apple's Pallas."""

    platform: str
    build: str
    display_version: str
    released: str
    beta: bool
    rc: bool
    source_path: str

    @classmethod
    def from_blob(
        cls,
        raw: bytes,
        *,
        expected: _RequiredRelease,
        source_path: str,
    ) -> ReleaseRecord:
        context = f"AppleDB {source_path}"
        data = parse_json_object(raw, context)
        required = {"osStr", "version", "build", "released"}
        missing = required - set(data)
        if missing:
            raise CatalogError(f"{context} is missing required keys: {sorted(missing)}")
        platform = _string(data["osStr"], f"{context}.osStr")
        if platform != expected.appledb_platform:
            raise CatalogError(
                f"{context}.osStr differs: expected {expected.appledb_platform}, got {platform}"
            )
        build = _string(data["build"], f"{context}.build")
        if build != expected.build or _BUILD.fullmatch(build) is None:
            raise CatalogError(f"{context}.build differs: expected {expected.build}, got {build}")
        display_version = _string(data["version"], f"{context}.version")
        base_version, separator, raw_qualifier = display_version.partition(" ")
        if _BASE_VERSION.fullmatch(base_version) is None:
            raise CatalogError(f"{context}.version has an unsupported base version")
        if base_version != expected.version:
            raise CatalogError(
                f"{context}.version differs: expected base {expected.version}, "
                f"got {display_version}"
            )
        beta = _boolean(data.get("beta", False), f"{context}.beta")
        rc = _boolean(data.get("rc", False), f"{context}.rc")
        if beta and rc:
            raise CatalogError(f"{context} cannot be both beta and RC")
        qualifier = raw_qualifier if separator else None
        expected_qualifier = _BETA_QUALIFIER if beta else _RC_QUALIFIER if rc else None
        if expected_qualifier is None:
            if qualifier is not None:
                raise CatalogError(f"{context}.version qualifier conflicts with release flags")
        elif qualifier is None or expected_qualifier.fullmatch(qualifier) is None:
            raise CatalogError(f"{context}.version qualifier conflicts with release flags")
        released = _released(data["released"], f"{context}.released")
        return cls(
            platform=expected.catalog_platform,
            build=build,
            display_version=display_version,
            released=released,
            beta=beta,
            rc=rc,
            source_path=source_path,
        )

    def to_object(self) -> JsonObject:
        channel = "beta" if self.beta else "rc" if self.rc else "release"
        return {
            "platform": self.platform,
            "build": self.build,
            "display_version": self.display_version,
            "channel": channel,
            "beta": self.beta,
            "rc": self.rc,
            "released": self.released,
            "source_path": self.source_path,
        }


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise CatalogError(f"{context} must be a non-empty string")
    return value


def _boolean(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise CatalogError(f"{context} must be a boolean")
    return value


def _released(value: object, context: str) -> str:
    released = _string(value, context)
    try:
        if "T" in released:
            datetime.fromisoformat(released)
        else:
            date.fromisoformat(released)
    except ValueError as error:
        raise CatalogError(f"{context} must be an ISO 8601 date or timestamp") from error
    return released


def _required_releases(entries_dir: Path) -> dict[tuple[str, str], _RequiredRelease]:
    required: dict[tuple[str, str], _RequiredRelease] = {}
    for entry in load_entries(entries_dir):
        source_platform = appledb_platform(entry.platform, entry.device)
        for release in (entry.previous, entry.next):
            key = (entry.platform, release.build)
            observed = required.get(key)
            expected = _RequiredRelease(
                catalog_platform=entry.platform,
                build=release.build,
                version=release.version,
                appledb_platform=source_platform,
            )
            if observed is not None and observed != expected:
                raise CatalogError(
                    f"catalog build {entry.platform} {release.build} conflicts: "
                    f"{observed} and {expected}"
                )
            required[key] = expected
    return required


@dataclass(frozen=True)
class PallasLabelSources:
    """Pallas inventories that label builds AppleDB lacks, and the day they were fetched."""

    paths: tuple[Path, ...] = ()
    observed_on: date | None = None


class AppleDBCoverageMissingError(CatalogError):
    """AppleDB has no record yet for a catalog build; publication should retry later."""


def _candidate_paths(
    repo: Path,
    commit: str,
    required: dict[tuple[str, str], _RequiredRelease],
) -> dict[tuple[str, str], list[str]]:
    candidates = {key: [] for key in required}
    keys_by_platform_and_build: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for key, requirement in required.items():
        keys_by_platform_and_build.setdefault((requirement.appledb_platform, key[1]), []).append(
            key
        )
    for source_platform in sorted({item[0] for item in keys_by_platform_and_build}):
        root = f"osFiles/{source_platform}"
        for entry_path in tree_paths(repo, commit, root):
            path = PurePosixPath(entry_path)
            if path.suffix != ".json":
                continue
            for key in keys_by_platform_and_build.get((source_platform, path.stem), []):
                candidates[key].append(entry_path)
    return candidates


def _pallas_record(
    requirement: _RequiredRelease,
    entries: list[tuple[JsonObject, str]],
    observed_on: date | None,
) -> ReleaseRecord | None:
    """Labels a build from Pallas when every OTA Apple serves for it agrees on the label."""
    labels: set[tuple[str, str, str, object]] = set()
    for entry, _context in entries:
        if entry.get("os") != requirement.appledb_platform:
            continue
        if entry.get("build") != requirement.build:
            continue
        label = display_version(entry)
        channel = entry.get("channel")
        if label is None or not isinstance(channel, dict):
            return None
        documentation_id = channel.get("documentation_id")
        if not isinstance(documentation_id, str):
            return None
        labels.add((label, str(channel.get("kind")), documentation_id, entry.get("posting_date")))
    if len(labels) != 1:
        return None
    label, kind, documentation_id, posting_date = labels.pop()
    if label.partition(" ")[0] != requirement.version:
        return None
    if isinstance(posting_date, str):
        released = _released(posting_date, f"Pallas {requirement.build} posting_date")
    elif observed_on is not None:
        # Apple publishes no posting date for seeds; the poller sees a build the day it ships.
        released = observed_on.isoformat()
    else:
        return None
    return ReleaseRecord(
        platform=requirement.catalog_platform,
        build=requirement.build,
        display_version=label,
        released=released,
        beta=kind == "beta",
        rc=kind == "rc",
        source_path=f"pallas:{documentation_id}",
    )


def _load_records(
    repo: Path,
    commit: str,
    required: dict[tuple[str, str], _RequiredRelease],
    pallas: PallasLabelSources,
) -> tuple[ReleaseRecord, ...]:
    candidates = _candidate_paths(repo, commit, required)
    missing = sorted(key for key, paths in candidates.items() if not paths)
    ambiguous = sorted((key, paths) for key, paths in candidates.items() if len(paths) > 1)
    if ambiguous:
        raise CatalogError(f"AppleDB coverage differs: missing={missing}, ambiguous={ambiguous}")
    records = [
        ReleaseRecord.from_blob(
            blob_at_path(repo, commit, candidates[key][0]),
            expected=requirement,
            source_path=candidates[key][0],
        )
        for key, requirement in required.items()
        if key not in missing
    ]
    entries = read_pallas_entries(pallas.paths) if missing else []
    labeled = {
        key: record
        for key in missing
        if (record := _pallas_record(required[key], entries, pallas.observed_on)) is not None
    }
    # A stale documentation ID would repeat an earlier seed's label, so a label another
    # build already has means waiting for AppleDB's curated one.
    taken = [(record.platform, record.display_version) for record in records]
    taken += [(record.platform, record.display_version) for record in labeled.values()]
    unlabeled = [
        key
        for key in missing
        if key not in labeled
        or taken.count((labeled[key].platform, labeled[key].display_version)) > 1
    ]
    if unlabeled:
        raise AppleDBCoverageMissingError(
            f"AppleDB coverage differs: missing={unlabeled}, ambiguous=[]"
        )
    records.extend(labeled.values())
    return tuple(sorted(records, key=lambda item: (_PLATFORM_ORDER[item.platform], item.build)))


def _registry_object(commit: str, records: tuple[ReleaseRecord, ...]) -> JsonObject:
    return {
        "schema_version": 1,
        "source": {"repository": _APPLEDB_REPOSITORY, "commit": commit},
        "releases": [record.to_object() for record in records],
    }


def _write_or_check(path: Path, content: str, *, check: bool) -> None:
    if check:
        try:
            observed = path.read_text(encoding="utf-8")
        except OSError as error:
            raise CatalogError(f"cannot check release metadata {path}: {error}") from error
        if observed != content:
            raise CatalogError(f"generated release metadata is stale: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def import_release_metadata(  # noqa: PLR0913 - the CLI's flags, passed through
    entries_dir: Path,
    appledb_repo: Path,
    appledb_commit: str,
    output: Path,
    *,
    check: bool,
    pallas: PallasLabelSources | None = None,
) -> ReleaseMetadataResult:
    """Writes or checks the release registry for every catalog endpoint.

    Builds AppleDB has not recorded yet are labeled from the `ipsw dl ota --json` files in
    `pallas`; seeds without a Pallas posting date get its `observed_on` day. A build that
    neither source labels raises `AppleDBCoverageMissingError`, so callers wait and retry.
    """
    repo = ensure_repository(appledb_repo)
    require_origin(repo, _APPLEDB_REPOSITORY, "AppleDB")
    commit = resolve_commit(repo, appledb_commit)
    required = _required_releases(entries_dir)
    records = _load_records(repo, commit, required, pallas or PallasLabelSources())
    _write_or_check(output, canonical_json(_registry_object(commit, records)), check=check)
    return ReleaseMetadataResult(
        output=output,
        release_count=len(records),
        beta_count=sum(record.beta for record in records),
        rc_count=sum(record.rc for record in records),
        checked=check,
    )
