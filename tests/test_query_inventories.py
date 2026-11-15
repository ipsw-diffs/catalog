from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.helpers import commit_all, init_repo

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/query-source-inventories.sh"
APPLEDB_ENVELOPE = '{"schema_version": 1, "releases": [{"build": "A1"}]}'
PALLAS_ENVELOPE = '{"schema_version": 1, "otas": []}'


def _run(
    tmp_path: Path, *, ota_fallback: str, ipsw_body: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    # Real jq, git and perl; only the ipsw CLI is replaced.
    appledb = tmp_path / "ipsw-config/appledb"
    appledb.parent.mkdir()
    init_repo(appledb)
    (appledb / "README.md").write_text("fixture\n")
    commit_all(appledb, "AppleDB fixture")
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
            "APPLEDB_PLATFORM": "macOS",
            "DEVICE": "Mac1,1",
            "MAJOR_VERSION": "27",
            "OTA_FALLBACK": ota_fallback,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    arguments = tmp_path / "discovery-args"
    lines = arguments.read_text().splitlines() if arguments.exists() else []
    return result, lines


_SERVE_ALL = f"""
case "$*" in
  *"dl appledb"*) printf '%s' '{APPLEDB_ENVELOPE}' ;;
  *"dl ota"*) printf '%s' '{PALLAS_ENVELOPE}' ;;
  *) exit 9 ;;
esac
"""


def test_ipsw_only_track_queries_only_ipsw_sources(tmp_path: Path) -> None:
    result, arguments = _run(tmp_path, ota_fallback="false", ipsw_body=_SERVE_ALL)

    assert result.returncode == 0, result.stderr
    assert arguments == ["--ipsw-sources", f"{tmp_path}/ipsw-sources.json"]
    assert len((tmp_path / "appledb-commit").read_text().strip()) == 40  # noqa: PLR2004
    calls = (tmp_path / "ipsw-calls").read_text()
    assert "--type ipsw" in calls
    assert "dl ota" not in calls


def test_ota_track_adds_full_otas_and_three_pallas_audiences(tmp_path: Path) -> None:
    result, arguments = _run(tmp_path, ota_fallback="true", ipsw_body=_SERVE_ALL)

    assert result.returncode == 0, result.stderr
    assert arguments == [
        "--ipsw-sources",
        f"{tmp_path}/ipsw-sources.json",
        "--ota-sources",
        f"{tmp_path}/ota-sources.json",
        "--pallas-sources",
        f"{tmp_path}/pallas-release.json",
        "--pallas-sources",
        f"{tmp_path}/pallas-rc.json",
        "--pallas-sources",
        f"{tmp_path}/pallas-beta.json",
    ]
    pallas_calls = [
        line for line in (tmp_path / "ipsw-calls").read_text().splitlines() if "dl ota" in line
    ]
    assert [call.split("--device Mac1,1 ")[1] for call in pallas_calls] == [
        "--json --no-color",
        "--rc --json --no-color",
        "--beta --json --no-color",
    ]
    assert all("--platform macos" in call for call in pallas_calls)


@pytest.mark.parametrize("failure", ["exit 1", "printf 'not json'"])
def test_failed_pallas_query_warns_and_keeps_appledb(tmp_path: Path, failure: str) -> None:
    body = f"""
case "$*" in
  *"dl appledb"*) printf '%s' '{APPLEDB_ENVELOPE}' ;;
  *"--rc"*) {failure} ;;
  *"dl ota"*) printf '%s' '{PALLAS_ENVELOPE}' ;;
esac
"""
    result, arguments = _run(tmp_path, ota_fallback="true", ipsw_body=body)

    assert result.returncode == 0, result.stderr
    assert f"{tmp_path}/pallas-rc.json" not in arguments
    assert f"{tmp_path}/pallas-release.json" in arguments
    assert not (tmp_path / "pallas-rc.json").exists()
    assert "::warning::Pallas rc query failed" in result.stdout


def test_empty_appledb_inventory_fails(tmp_path: Path) -> None:
    body = """
printf '%s' '{"schema_version": 1, "releases": []}'
"""
    result, _arguments = _run(tmp_path, ota_fallback="true", ipsw_body=body)

    assert result.returncode != 0
    assert "Expected nonempty ipsw AppleDB schema_version=1 ipsw releases envelope" in result.stderr
