from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "scripts/install-latest-ipsw.sh"
MAX_ATTEMPTS = 5
TRANSIENT_ATTEMPTS = 3
# Fails until the last attempt and leaves a partial file behind, so a retry has to overwrite it.
TRANSIENT_FAILURE = f"""
if [ "$(wc -l < "$FIXTURE/download-attempts")" -lt {TRANSIENT_ATTEMPTS} ]; then
  printf 'partial' > "$output"
  exit 1
fi
"""


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "api-failure",
        "prerelease",
        "missing-asset",
        "missing-digest",
        "bad-checksum",
        "wrong-version",
        "download-failure",
        "download-transient",
    ],
)
def test_install_latest_ipsw(tmp_path: Path, scenario: str) -> None:
    # Real extraction and hashing of a tiny executable; only `gh` and retry timing are mocked.
    binary_version = "3.1.708" if scenario == "wrong-version" else "3.1.720"
    payload = f'#!/bin/sh\nprintf "Version: {binary_version}, BuildCommit: abc123\\n"\n'.encode()
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("ipsw")
        member.size = len(payload)
        member.mode = 0o755
        tar.addfile(member, io.BytesIO(payload))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    digest = "sha256:" + ("0" * 64 if scenario == "bad-checksum" else checksum)
    asset = {
        "name": "ipsw_3.1.720_linux_x86_64.tar.gz",
        "state": "uploaded",
        "digest": None if scenario == "missing-digest" else digest,
    }
    (tmp_path / "release.json").write_text(
        json.dumps(
            {
                "tag_name": "v3.1.720",
                "draft": False,
                "prerelease": scenario == "prerelease",
                "assets": [] if scenario == "missing-asset" else [asset],
            }
        )
    )
    mock_bin = tmp_path / "mocks"
    mock_bin.mkdir()
    api_response = "exit 1" if scenario == "api-failure" else 'cat "$FIXTURE/release.json"'
    download_failures = {"download-failure": "exit 1", "download-transient": TRANSIENT_FAILURE}
    clients = {
        "gh": f"""
if [ "$1" = api ]; then
  test "$*" = "api repos/blacktop/ipsw/releases/latest" || exit 9
  {api_response}
  exit 0
fi
printf 'attempt\\n' >> "$FIXTURE/download-attempts"
test "$1 $2 $3" = "release download v3.1.720" || exit 9
test "$4 $5" = "--repo blacktop/ipsw" || exit 9
test "$6 $7" = "--pattern ipsw_3.1.720_linux_x86_64.tar.gz" || exit 9
test "$8" = --output || exit 9
test "${{10}}" = --clobber || exit 9
output=$9
{download_failures.get(scenario, ":")}
cp "$FIXTURE/release.tar.gz" "$output"
""",
        "timeout": """
test "$1" = 300 || exit 9
printf 'attempt\\n' >> "$FIXTURE/timed-attempts"
shift
exec "$@"
""",
        "sleep": "exit 0\n",
    }
    for name, body in clients.items():
        executable = mock_bin / name
        executable.write_text("#!/bin/sh\nset -eu\n" + body)
        executable.chmod(0o755)
    github_env = tmp_path / "github-env"
    github_path = tmp_path / "github-path"
    github_env.touch()
    github_path.touch()
    result = subprocess.run(  # noqa: S603
        ["bash", str(INSTALLER)],
        env={
            **os.environ,
            "PATH": f"{mock_bin}{os.pathsep}{os.defpath}:/opt/homebrew/bin",
            "FIXTURE": str(tmp_path),
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_ENV": str(github_env),
            "GITHUB_PATH": str(github_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if scenario in {"success", "download-transient"}:
        assert result.returncode == 0, result.stderr
        assert github_env.read_text() == (f"IPSW_VERSION=3.1.720\nIPSW_ARCHIVE_SHA256={checksum}\n")
        assert (Path(github_path.read_text().strip()) / "ipsw").is_file()
        assert "Version: 3.1.720" in result.stdout
    else:
        assert result.returncode != 0
        assert github_env.read_text() == ""
        assert github_path.read_text() == ""
    attempts = tmp_path / "download-attempts"
    if attempts.exists():
        assert (tmp_path / "timed-attempts").read_text() == attempts.read_text()
    if scenario == "download-failure":
        assert len(attempts.read_text().splitlines()) == MAX_ATTEMPTS
        assert f"after {MAX_ATTEMPTS} attempts" in result.stderr
    if scenario == "download-transient":
        assert len(attempts.read_text().splitlines()) == TRANSIENT_ATTEMPTS
