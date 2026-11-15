from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/query-pallas-labels.sh"
ENVELOPE = '{"schema_version": 1, "otas": []}'


def _entry(name: str, platform: str, device: str, builds: tuple[str, str]) -> tuple[str, str]:
    value = {
        "platform": platform,
        "device": device,
        "from": {"build": builds[0]},
        "to": {"build": builds[1]},
    }
    return name, json.dumps(value)


def _run(
    tmp_path: Path, *, known: list[str], ipsw_body: str = f"echo '{ENVELOPE}'\n"
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    # Real jq and perl; only the ipsw CLI is replaced.
    entries = tmp_path / "entries"
    entries.mkdir()
    for name, text in (
        _entry("ios-old.json", "iOS", "iPhone18,1", ("A1", "A2")),
        _entry("ios-new.json", "iOS", "iPhone18,1", ("A2", "A3")),
        _entry("macos-old.json", "macOS", "Mac17,6", ("M1", "M2")),
    ):
        (entries / name).write_text(text)
    registry = tmp_path / "releases.json"
    registry.write_text(
        json.dumps(
            {"releases": [{"platform": key.split()[0], "build": key.split()[1]} for key in known]}
        )
    )
    mocks = tmp_path / "mocks"
    mocks.mkdir()
    ipsw = mocks / "ipsw"
    ipsw.write_text(
        '#!/bin/sh\nset -eu\nprintf \'%s\\n\' "$*" >> "$RUNNER_TEMP/ipsw-calls"\n' + ipsw_body
    )
    ipsw.chmod(0o755)
    result = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT)],
        env={
            **os.environ,
            "PATH": f"{mocks}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "ENTRIES_DIR": str(entries),
            "REGISTRY": str(registry),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    calls_file = tmp_path / "ipsw-calls"
    calls = calls_file.read_text().splitlines() if calls_file.exists() else []
    arguments_file = tmp_path / "release-metadata-args"
    arguments = arguments_file.read_text().splitlines() if arguments_file.exists() else []
    return result, calls, arguments


def test_queries_pallas_only_for_devices_with_unlabeled_builds(tmp_path: Path) -> None:
    result, calls, arguments = _run(tmp_path, known=["iOS A1", "iOS A2", "macOS M1", "macOS M2"])

    assert result.returncode == 0, result.stderr
    assert [call.split(" --json")[0].split("dl ota ")[1] for call in calls] == [
        "--platform ios --device iPhone18,1",
        "--platform ios --device iPhone18,1 --rc",
        "--platform ios --device iPhone18,1 --beta",
    ]
    assert arguments[:2] == ["--observed-on", datetime.now(UTC).date().isoformat()]
    assert arguments[2:] == [
        item
        for audience in ("release", "rc", "beta")
        for item in ("--pallas-sources", f"{tmp_path}/pallas-labels/iPhone18,1-{audience}.json")
    ]


def test_queries_nothing_when_every_build_is_labeled(tmp_path: Path) -> None:
    result, calls, arguments = _run(
        tmp_path, known=["iOS A1", "iOS A2", "iOS A3", "macOS M1", "macOS M2"]
    )

    assert result.returncode == 0, result.stderr
    assert calls == []
    assert arguments[0] == "--observed-on"
    assert len(arguments) == 2  # noqa: PLR2004


def test_failed_pallas_query_leaves_labels_to_appledb(tmp_path: Path) -> None:
    result, _calls, arguments = _run(
        tmp_path,
        known=["iOS A1", "iOS A2", "macOS M1", "macOS M2"],
        ipsw_body='case "$*" in *--beta*) exit 1 ;; *) echo \'' + ENVELOPE + "' ;; esac\n",
    )

    assert result.returncode == 0, result.stderr
    assert "::warning::Pallas beta query for iPhone18,1 failed" in result.stdout
    assert f"{tmp_path}/pallas-labels/iPhone18,1-beta.json" not in arguments
    assert not (tmp_path / "pallas-labels/iPhone18,1-beta.json").exists()
    assert arguments.count("--pallas-sources") == 2  # noqa: PLR2004
