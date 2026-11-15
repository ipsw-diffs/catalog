from __future__ import annotations

import json
from pathlib import Path

import pytest

from ipsw_diff_catalog.model import CatalogError, JsonObject
from ipsw_diff_catalog.pallas import read_pallas

_CAPTURED = Path(__file__).parent / "fixtures/pallas-v3.1.747-mac17-6-release.json"
_DIGEST = "a" * 40


def _entry(  # noqa: PLR0913
    build: str = "B2",
    *,
    version: str = "27.1.1",
    kind: str | None = "release",
    posting_date: str | None = "2026-09-08",
    sha256: str | None = "b" * 64,
    url_digest: str = _DIGEST,
    devices: tuple[str, ...] = ("Mac1,1",),
) -> JsonObject:
    return {
        "os": "macOS",
        "version": version,
        "version_extra": None,
        "build": build,
        "channel": {"kind": kind},
        "posting_date": posting_date,
        "delivery": "full",
        "prerequisite": None,
        "supported_devices": list(devices),
        "url": f"https://updates.cdn-apple.com/fixture/{url_digest}.zip",
        "download_size": 5000,
        "sha1": url_digest,
        "sha256": sha256,
    }


def _write(path: Path, *entries: JsonObject) -> Path:
    path.write_text(json.dumps({"schema_version": 1, "otas": list(entries)}), encoding="utf-8")
    return path


def _read(*paths: Path):  # noqa: ANN202
    return read_pallas(paths, platform="macOS", device="Mac1,1", major=27)


def test_read_captured_ipsw_v3747_envelope() -> None:
    # Unmodified `ipsw dl ota --platform macos --device Mac17,6 --json` output, 2026-11-15.
    # Apple serves 25G342 to the release audience under documentation ID macOS2673RC.
    inventory = read_pallas((_CAPTURED,), platform="macOS", device="Mac17,6", major=26)

    assert inventory.deferred == ()
    assert [build.build for build in inventory.builds] == ["25G342"]
    build = inventory.builds[0]
    assert build.record == {
        "version": "26.7.3",
        "build": "25G342",
        "released": "2026-11-09",
        "beta": False,
        "rc": False,
    }
    assert build.source["hashes"] == {
        "sha2-256": "8817c30dcc1b144a283f7bf1dc0867a969d1d9185d10a6e41ca075aaeaa6f18c"
    }
    expected_size = 18361040129
    assert build.source["size"] == expected_size
    other_major = read_pallas((_CAPTURED,), platform="macOS", device="Mac17,6", major=27)
    assert [build.build for build in other_major.builds] == ["26B131"]


@pytest.mark.parametrize(
    ("kind", "label", "beta", "rc"),
    [("release", "27.1.1", False, False), ("rc", "27.1.1 RC", False, True)],
)
def test_read_labels_builds_from_apple_channel(
    tmp_path: Path, kind: str, label: str, *, beta: bool, rc: bool
) -> None:
    inventory = _read(_write(tmp_path / "pallas.json", _entry(kind=kind)))

    assert inventory.builds[0].record == {
        "version": label,
        "build": "B2",
        "released": "2026-09-08",
        "beta": beta,
        "rc": rc,
    }


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (_entry(kind="unknown"), "Apple's channel for it is 'unknown'"),
        (_entry(kind=None), "Apple's channel for it is None"),
        (_entry(posting_date=None), "Apple published no posting date"),
        (_entry(sha256=None), "Apple published no SHA-256"),
    ],
)
def test_read_defers_builds_apple_describes_incompletely(
    tmp_path: Path, entry: JsonObject, reason: str
) -> None:
    inventory = _read(_write(tmp_path / "pallas.json", entry))

    assert inventory.builds == ()
    assert [(item.build, item.reason) for item in inventory.deferred] == [("B2", reason)]


def test_read_defers_a_build_with_two_full_otas(tmp_path: Path) -> None:
    inventory = _read(
        _write(tmp_path / "pallas.json", _entry(), _entry(url_digest="c" * 40)),
    )

    assert inventory.builds == ()
    assert inventory.deferred[0].reason == "Apple serves 2 full OTAs"


def test_read_merges_one_url_seen_by_several_audience_queries(tmp_path: Path) -> None:
    release = _write(tmp_path / "release.json", _entry())
    rc = _write(tmp_path / "rc.json", _entry())

    inventory = _read(release, rc)

    assert [build.build for build in inventory.builds] == ["B2"]
    assert inventory.deferred == ()


def test_read_ignores_entries_outside_the_track(tmp_path: Path) -> None:
    delta = _entry("B3")
    delta["delivery"] = "delta"
    delta["prerequisite"] = "B2"
    unlinked_delta = _entry("B8")
    unlinked_delta["delivery"] = "delta"
    rsr = _entry("B4")
    rsr["version_extra"] = "(a)"
    other_os = _entry("B5")
    other_os["os"] = "iOS"
    inventory = _read(
        _write(
            tmp_path / "pallas.json",
            delta,
            unlinked_delta,
            rsr,
            other_os,
            _entry("B6", devices=("Other1,1",)),
            _entry("B7", version="26.7.3"),
        )
    )

    assert inventory.builds == ()
    assert inventory.deferred == ()


@pytest.mark.parametrize(
    "value",
    [[], {}, {"schema_version": 2, "otas": []}, {"schema_version": 1, "otas": {}}],
)
def test_read_rejects_a_changed_envelope(tmp_path: Path, value: object) -> None:
    path = tmp_path / "pallas.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(CatalogError):
        _read(path)
