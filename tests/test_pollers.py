from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.helpers import commit_all, git, init_repo

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = {"status": "candidate", "edges": [{"to": {"build": "B2"}}]}


def _mock(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)


def _shard(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(  # noqa: S603
        ["git", "init", "--quiet", "--bare", "--initial-branch=main", str(origin)],
        check=True,
    )
    shard = tmp_path / "work/shard"
    shard.parent.mkdir()
    init_repo(shard)
    (shard / "track.json").write_text("{}\n")
    commit_all(shard, "shard fixture")
    git(shard, "branch", "-M", "main")
    git(shard, "remote", "add", "origin", str(origin))
    git(shard, "push", "--quiet", "origin", "main")
    return shard


def _poll_discovery(
    tmp_path: Path,
    *,
    decision: object | None,
    paused: str | None = "0",
    dispatch_fails: bool = False,
    generated_branch: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    shard = _shard(tmp_path)
    if generated_branch:
        git(shard, "push", "--quiet", "origin", "HEAD:refs/heads/generated/macos-26-B2")
    work = shard.parent
    scripts = work / "catalog/scripts"
    scripts.mkdir(parents=True)
    _mock(
        scripts,
        "query-source-inventories.sh",
        "printf -- '--ipsw-sources\\n%s\\n' x > \"$RUNNER_TEMP/discovery-args\"\n"
        "printf '%040d\\n' 0 > \"$RUNNER_TEMP/appledb-commit\"\n",
    )
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    if decision is not None:
        (fixture / "decision.json").write_text(json.dumps(decision))
    mocks = tmp_path / "mocks"
    mocks.mkdir()
    _mock(mocks, "uv", 'cat "$FIXTURE/decision.json"\n')
    issue_list = (
        "echo 'the repository has disabled issues' >&2; exit 1"
        if paused is None
        else f"printf '%s\\n' {paused}"
    )
    _mock(
        mocks,
        "gh",
        f"""
case "$1 $2" in
  "issue list") {issue_list} ;;
  "workflow run")
    printf '%s\\n' "$3" >> "$FIXTURE/dispatches"
    {"exit 1" if dispatch_fails else ":"} ;;
  *) exit 9 ;;
esac
""",
    )
    result = subprocess.run(  # noqa: S603
        ["bash", str(ROOT / "scripts/poll-discovery.sh")],
        cwd=work,
        env={
            **os.environ,
            "PATH": f"{mocks}{os.pathsep}{os.environ['PATH']}",
            "FIXTURE": str(fixture),
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_REPOSITORY": "ipsw-diffs/macos-26",
            "GH_TOKEN": "unused",
            "POLICY_PATH": "track.json",
            "MANIFESTS_DIR": "manifests",
            "TRACK_ID": "macos-26",
            "DISCOVER_WORKFLOW": "discover.yml",
            "POLL_INTERVAL": "1",
            "POLL_DURATION": "3",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    dispatches = fixture / "dispatches"
    return result, dispatches.read_text().splitlines() if dispatches.exists() else []


def test_discovery_poller_dispatches_a_new_build_once(tmp_path: Path) -> None:
    result, dispatches = _poll_discovery(tmp_path, decision=CANDIDATE)

    assert result.returncode == 0, result.stderr
    assert dispatches == ["discover.yml"]
    assert "dispatched discover.yml for macos-26 build B2" in result.stdout


@pytest.mark.parametrize(
    ("decision", "paused"),
    [({"status": "current", "edges": []}, "0"), (CANDIDATE, "1")],
    ids=["current", "paused-by-failure-issue"],
)
def test_discovery_poller_leaves_current_and_paused_shards_alone(
    tmp_path: Path, decision: object, paused: str
) -> None:
    result, dispatches = _poll_discovery(tmp_path, decision=decision, paused=paused)

    assert result.returncode == 0, result.stderr
    assert dispatches == []


def test_discovery_poller_skips_a_build_with_a_generated_branch(tmp_path: Path) -> None:
    result, dispatches = _poll_discovery(tmp_path, decision=CANDIDATE, generated_branch=True)

    assert result.returncode == 0, result.stderr
    assert dispatches == []


def test_discovery_poller_warns_when_it_cannot_read_the_pause_issue(tmp_path: Path) -> None:
    result, dispatches = _poll_discovery(tmp_path, decision=CANDIDATE, paused=None)

    assert result.returncode == 0, result.stderr
    assert dispatches == []
    assert "::warning::could not read ipsw-diffs/macos-26 generation-failed issues" in result.stdout
    assert "build B2 stays queued" in result.stdout


def test_discovery_poller_survives_failed_discovery(tmp_path: Path) -> None:
    result, dispatches = _poll_discovery(tmp_path, decision=None)

    assert result.returncode == 0, result.stderr
    assert dispatches == []
    assert "::warning::discovery failed" in result.stdout


def test_discovery_poller_retries_a_failed_dispatch(tmp_path: Path) -> None:
    result, dispatches = _poll_discovery(tmp_path, decision=CANDIDATE, dispatch_fails=True)

    assert result.returncode == 0, result.stderr
    assert len(dispatches) >= 2  # noqa: PLR2004
    assert "::warning::could not dispatch discover.yml for build B2" in result.stdout


def _poll_catalog(
    tmp_path: Path, *, moving_head: bool, republish: str, api_fails: bool = False
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    automation = tmp_path / "work/automation"
    automation.mkdir(parents=True)
    (automation / "shards.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "shards": [
                    {
                        "id": shard,
                        "repository": f"https://github.com/ipsw-diffs/{shard}",
                        "branch": "main",
                    }
                    for shard in ("macos-15", "macos-26")
                ],
            }
        )
    )
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    mocks = tmp_path / "mocks"
    mocks.mkdir()
    head = (
        'printf x >> "$FIXTURE/api-calls"; printf "%s\\n" "$(wc -c < "$FIXTURE/api-calls")"'
        if moving_head
        else "printf 'aaaa\\n'"
    )
    _mock(
        mocks,
        "gh",
        f"""
case "$1" in
  api) {"exit 1" if api_fails else head} ;;
  workflow) printf '%s\\n' "$3" >> "$FIXTURE/dispatches" ;;
  *) exit 9 ;;
esac
""",
    )
    result = subprocess.run(  # noqa: S603
        ["bash", str(ROOT / "scripts/poll-catalog.sh")],
        cwd=tmp_path / "work",
        env={
            **os.environ,
            "PATH": f"{mocks}{os.pathsep}{os.environ['PATH']}",
            "FIXTURE": str(fixture),
            "GITHUB_REPOSITORY": "ipsw-diffs/catalog",
            "GH_TOKEN": "unused",
            "POLL_INTERVAL": "1",
            "POLL_DURATION": "3",
            "REPUBLISH_INTERVAL": republish,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    dispatches = fixture / "dispatches"
    return result, dispatches.read_text().splitlines() if dispatches.exists() else []


def test_catalog_poller_publishes_once_while_shards_stay_put(tmp_path: Path) -> None:
    result, dispatches = _poll_catalog(tmp_path, moving_head=False, republish="100")

    assert result.returncode == 0, result.stderr
    assert dispatches == ["catalog-publish.yml"]


def test_catalog_poller_publishes_again_when_a_shard_moves(tmp_path: Path) -> None:
    result, dispatches = _poll_catalog(tmp_path, moving_head=True, republish="100")

    assert result.returncode == 0, result.stderr
    assert len(dispatches) >= 2  # noqa: PLR2004


def test_catalog_poller_retries_waiting_publication_on_its_interval(tmp_path: Path) -> None:
    result, dispatches = _poll_catalog(tmp_path, moving_head=False, republish="1")

    assert result.returncode == 0, result.stderr
    assert len(dispatches) >= 2  # noqa: PLR2004


def test_catalog_poller_does_not_publish_on_unreadable_heads(tmp_path: Path) -> None:
    result, dispatches = _poll_catalog(tmp_path, moving_head=False, republish="100", api_fails=True)

    assert result.returncode == 0, result.stderr
    assert dispatches == []
    assert "::warning::could not read every shard head" in result.stdout
