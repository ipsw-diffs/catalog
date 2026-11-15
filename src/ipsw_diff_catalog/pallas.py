"""Reads Apple's live Pallas OTA inventory, as printed by `ipsw dl ota --json`."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ipsw_diff_catalog.model import CatalogError, JsonObject

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_CHANNEL_LABELS = {"release": "", "rc": " RC", "beta": " beta"}
_MAJOR = re.compile(r"([0-9]+)(?:\.|$)")


@dataclass(frozen=True)
class PallasBuild:
    """One full OTA that Apple serves for the track, in AppleDB record shape."""

    record: JsonObject
    source: JsonObject
    context: str

    @property
    def build(self) -> str:
        return str(self.record["build"])


@dataclass(frozen=True)
class PallasDeferral:
    """A Pallas build left for AppleDB, and why."""

    build: str
    reason: str

    def to_object(self) -> JsonObject:
        return {"build": self.build, "reason": self.reason}


@dataclass(frozen=True)
class PallasInventory:
    builds: tuple[PallasBuild, ...]
    deferred: tuple[PallasDeferral, ...]


def _object(value: object, context: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CatalogError(f"{context} must be an object")
    return cast("JsonObject", value)


def _entries(path: Path) -> list[tuple[JsonObject, str]]:
    try:
        value: object = json.loads(path.read_bytes())
    except OSError as error:
        raise CatalogError(f"cannot read Pallas OTA inventory {path}: {error}") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CatalogError(f"Pallas OTA inventory is not valid JSON: {path}") from error
    envelope = _object(value, f"Pallas OTA inventory {path}")
    if type(envelope.get("schema_version")) is not int or envelope["schema_version"] != 1:
        raise CatalogError(f"Pallas OTA inventory {path} schema_version must be 1")
    otas = envelope.get("otas")
    if not isinstance(otas, list):
        raise CatalogError(f"Pallas OTA inventory {path} otas must be an array")
    return [
        (_object(item, f"{path} otas[{index}]"), f"{path} otas[{index}]")
        for index, item in enumerate(otas)
    ]


def _selects_track(entry: JsonObject, platform: str, device: str, major: int) -> bool:
    version = entry.get("version")
    devices = entry.get("supported_devices")
    match = _MAJOR.match(version) if isinstance(version, str) else None
    return (
        entry.get("os") == platform
        and match is not None
        and int(match.group(1)) == major
        and entry.get("delivery") == "full"
        and entry.get("prerequisite") is None
        and entry.get("version_extra") is None
        and isinstance(devices, list)
        and device in devices
    )


def _deferral_reason(entry: JsonObject) -> str | None:
    channel = entry.get("channel")
    kind = channel.get("kind") if isinstance(channel, dict) else None
    if kind not in _CHANNEL_LABELS:
        return f"Apple's channel for it is {kind!r}"
    if not isinstance(entry.get("posting_date"), str):
        return "Apple published no posting date"
    if not isinstance(entry.get("sha256"), str):
        return "Apple published no SHA-256"
    return None


def _build(entry: JsonObject, context: str) -> PallasBuild:
    channel = cast("JsonObject", entry["channel"])
    kind = str(channel["kind"])
    record: JsonObject = {
        "version": f"{entry.get('version')}{_CHANNEL_LABELS[kind]}",
        "build": entry.get("build"),
        "released": entry["posting_date"],
        "beta": kind == "beta",
        "rc": kind == "rc",
    }
    source: JsonObject = {
        "type": "ota",
        "deviceMap": entry["supported_devices"],
        "links": [{"url": entry.get("url"), "active": True}],
        "hashes": {"sha2-256": entry["sha256"]},
        "size": entry.get("download_size"),
    }
    return PallasBuild(record=record, source=source, context=context)


def _unique_by_url(
    entries: Iterable[tuple[JsonObject, str]],
) -> dict[str, list[tuple[JsonObject, str]]]:
    """Groups track entries by build, keeping one entry per URL across audience queries."""
    by_url: dict[object, tuple[JsonObject, str]] = {}
    for entry, context in entries:
        by_url.setdefault(entry.get("url"), (entry, context))
    by_build: dict[str, list[tuple[JsonObject, str]]] = {}
    for entry, context in by_url.values():
        build = entry.get("build")
        if not isinstance(build, str):
            raise CatalogError(f"{context}.build must be a string")
        by_build.setdefault(build, []).append((entry, context))
    return by_build


def read_pallas(
    paths: tuple[Path, ...],
    *,
    platform: str,
    device: str,
    major: int,
) -> PallasInventory:
    """Returns the full OTAs Apple serves for one track, and the builds left for AppleDB."""
    track_entries = (
        (entry, context)
        for path in paths
        for entry, context in _entries(path)
        if _selects_track(entry, platform, device, major)
    )
    builds: list[PallasBuild] = []
    deferred: list[PallasDeferral] = []
    for build, entries in sorted(_unique_by_url(track_entries).items()):
        if len(entries) != 1:
            deferred.append(PallasDeferral(build, f"Apple serves {len(entries)} full OTAs"))
            continue
        entry, context = entries[0]
        reason = _deferral_reason(entry)
        if reason is not None:
            deferred.append(PallasDeferral(build, reason))
            continue
        builds.append(_build(entry, context))
    return PallasInventory(builds=tuple(builds), deferred=tuple(deferred))
