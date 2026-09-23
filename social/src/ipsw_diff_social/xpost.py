from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# xpost's own target names, in the order posts go out.
NETWORKS = ("bluesky", "mastodon", "twitter")
# Longer than xpost's own 180-second budget for X, so xpost reports its timeout first.
TIMEOUT_SECONDS = 300.0
_DETAIL_LIMIT = 500


class PublishError(RuntimeError):
    """xpost did not confirm the post. On X it may have gone out anyway."""


@dataclass(frozen=True)
class Post:
    message: str
    link: str
    image: Path
    alt_text: str

    @property
    def text(self) -> str:
        """The body xpost sends: the message, a blank line, then the link."""
        return f"{self.message}\n\n{self.link}"


@dataclass(frozen=True)
class XPost:
    """Runs the xpost CLI, which reads its credentials from the environment."""

    executable: Path
    timeout: float = TIMEOUT_SECONDS

    def validate(self, network: str, post: Post) -> None:
        """Check the post with `xpost --dry-run`, which needs no credentials or network."""
        self._run(network, post, dry_run=True)

    def publish(self, network: str, post: Post) -> None:
        self._run(network, post, dry_run=False)

    def _run(self, network: str, post: Post, *, dry_run: bool) -> None:
        if network not in NETWORKS:
            raise ValueError(f"unsupported network: {network}")
        # --name=value keeps a value that starts with "-" from reading as an option; the
        # message goes on stdin for the same reason.
        arguments = [
            str(self.executable),
            f"--target={network}",
            f"--link={post.link}",
            f"--image={post.image}",
            f"--alt-text={post.alt_text}",
        ]
        if dry_run:
            arguments.append("--dry-run")
        try:
            result = subprocess.run(
                arguments,
                input=post.message,
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise PublishError(f"xpost timed out after {self.timeout:g}s on {network}") from error
        if result.returncode != 0:
            output = " ".join((result.stderr or result.stdout).split())
            raise PublishError(
                f"xpost exited {result.returncode} on {network}: {output[-_DETAIL_LIMIT:]}"
            )
