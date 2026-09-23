from __future__ import annotations

import argparse
from pathlib import Path

from ipsw_diff_social.publisher import MODES, RunConfig, run
from ipsw_diff_social.xpost import NETWORKS, XPost


def _default_base_image() -> Path:
    return Path(__file__).resolve().parent / "assets" / "ipsw-diff-social-base.png"


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Publish new ipsw-diff catalog entries.")
    command.add_argument(
        "--mode",
        choices=MODES,
        default="dry-run",
        help=(
            "check reports whether there is work; dry-run renders and validates one entry; "
            "bootstrap suppresses all current entries; publish posts new ones"
        ),
    )
    command.add_argument("--base-image", type=Path, default=_default_base_image())
    command.add_argument("--output-dir", type=Path, default=Path("social-output"))
    command.add_argument("--xpost", type=Path, help="xpost executable (publish and dry-run)")
    command.add_argument(
        "--network",
        action="append",
        choices=NETWORKS,
        help="network to post to; repeat for several (default: all)",
    )
    command.add_argument("--entry-id", help="immutable catalog ID to render in dry-run mode")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    networks = tuple(network for network in NETWORKS if network in (args.network or NETWORKS))
    return run(
        RunConfig(
            mode=args.mode,
            base_image=args.base_image,
            output_dir=args.output_dir,
            xpost=XPost(args.xpost) if args.xpost is not None else None,
            networks=networks,
            entry_id=args.entry_id or None,
        )
    )
