from __future__ import annotations

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from ipsw_diff_social.xpost import Post, PublishError, XPost

# Records its arguments and stdin, then behaves as FAKE_XPOST_MODE asks.
FAKE_XPOST = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_XPOST_ARGS"
cat > "$FAKE_XPOST_STDIN"
case "$FAKE_XPOST_MODE" in
  fail) echo "Twitter/X: X did not confirm the post" >&2; exit 1 ;;
  hang) sleep 5 ;;
esac
"""


@pytest.fixture
def fake_xpost(tmp_path: Path, monkeypatch: MonkeyPatch) -> XPost:
    executable = tmp_path / "xpost"
    executable.write_text(FAKE_XPOST)
    executable.chmod(0o755)
    monkeypatch.setenv("FAKE_XPOST_ARGS", str(tmp_path / "args"))
    monkeypatch.setenv("FAKE_XPOST_STDIN", str(tmp_path / "stdin"))
    monkeypatch.setenv("FAKE_XPOST_MODE", "ok")
    # Generous: a new script's first launch can be slow on a busy machine.
    return XPost(executable, timeout=30)


def _post(tmp_path: Path) -> Post:
    return Post(
        message="New ipsw-diff: iOS 27.1 → iOS 27.2\n\n• Mach-O: 97 items updated",
        link="https://github.com/ipsw-diffs/ios-27/blob/abc/diffs/x/README.md",
        image=tmp_path / "card.jpg",
        alt_text="-starts with a dash",
    )


def test_publish_passes_options_as_single_arguments_and_the_message_on_stdin(
    fake_xpost: XPost, tmp_path: Path
) -> None:
    post = _post(tmp_path)

    fake_xpost.publish("twitter", post)

    assert (tmp_path / "args").read_text().splitlines() == [
        "--target=twitter",
        f"--link={post.link}",
        f"--image={post.image}",
        "--alt-text=-starts with a dash",
    ]
    assert (tmp_path / "stdin").read_text() == post.message


def test_validate_is_a_dry_run(fake_xpost: XPost, tmp_path: Path) -> None:
    fake_xpost.validate("bluesky", _post(tmp_path))

    arguments = (tmp_path / "args").read_text().splitlines()
    assert arguments[0] == "--target=bluesky"
    assert arguments[-1] == "--dry-run"


def test_a_failed_post_raises_with_xposts_error(
    fake_xpost: XPost, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_XPOST_MODE", "fail")

    with pytest.raises(PublishError, match="exited 1 on twitter: Twitter/X: X did not confirm"):
        fake_xpost.publish("twitter", _post(tmp_path))


def test_a_hung_xpost_is_reported_as_unconfirmed(
    fake_xpost: XPost, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_XPOST_MODE", "hang")
    slow = XPost(fake_xpost.executable, timeout=0.5)

    with pytest.raises(PublishError, match=r"timed out after 0\.5s on mastodon"):
        slow.publish("mastodon", _post(tmp_path))


def test_an_unknown_network_never_runs_xpost(fake_xpost: XPost, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported network: myspace"):
        fake_xpost.publish("myspace", _post(tmp_path))
    assert not (tmp_path / "args").exists()
