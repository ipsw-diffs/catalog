from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ipsw_diff_catalog.archive import render_archive
from ipsw_diff_catalog.audit import audit
from ipsw_diff_catalog.census import census
from ipsw_diff_catalog.discovery import discover
from ipsw_diff_catalog.model import CatalogError, MigrationSpec
from ipsw_diff_catalog.planner import plan
from ipsw_diff_catalog.reconcile import reconcile
from ipsw_diff_catalog.release_metadata import (
    AppleDBCoverageMissingError,
    import_release_metadata,
)
from ipsw_diff_catalog.render import render
from ipsw_diff_catalog.stage import (
    stage,
    stage_batch,
    validate_staged,
    validate_staged_batch,
)
from ipsw_diff_catalog.verify import materialize_manifest, record, verify


def _add_discover_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    discover_parser = commands.add_parser(
        "discover",
        help="queue every missing AppleDB edge after one immutable track anchor",
    )
    discover_parser.add_argument("--policy", type=Path, required=True)
    discover_parser.add_argument("--manifests-dir", type=Path, required=True)
    discover_parser.add_argument("--appledb-repo", type=Path, required=True)
    discover_parser.add_argument("--appledb-commit", required=True)
    discover_parser.add_argument("--ipsw-sources", type=Path, required=True)
    discover_parser.add_argument(
        "--ota-sources",
        type=Path,
        help="full-OTA inventory from `ipsw dl appledb --type ota`; required with ota_fallback",
    )
    discover_parser.add_argument(
        "--pallas-sources",
        type=Path,
        action="append",
        default=[],
        help="live OTA inventory from `ipsw dl ota --json`; repeat for each audience query",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipsw-diff-catalog",
        description="Integrity-first catalog tooling for ipsw-diff shards.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    verify_parser = commands.add_parser("verify", help="verify source and destination commits")
    _add_spec_and_repositories(verify_parser, destination=True)
    verify_parser.add_argument("--destination-revision", required=True)

    record_parser = commands.add_parser("record", help="verify and write one catalog entry")
    _add_spec_and_repositories(record_parser, destination=True)
    record_parser.add_argument("--destination-revision", required=True)
    record_parser.add_argument("--entries-dir", type=Path, default=Path("entries"))

    stage_parser = commands.add_parser(
        "stage",
        help="materialize and verify one source subtree in a clean shard worktree",
    )
    _add_spec_and_repositories(stage_parser, destination=True)
    stage_parser.add_argument("--destination-base", required=True)

    staged_parser = commands.add_parser(
        "validate-staged",
        help="re-verify a staged migration without modifying it",
    )
    _add_spec_and_repositories(staged_parser, destination=True)
    staged_parser.add_argument("--destination-base", required=True)

    batch_parser = commands.add_parser(
        "stage-batch",
        help="materialize and verify an all-or-rollback batch in one clean shard",
    )
    _add_batch_and_repositories(batch_parser)
    batch_parser.add_argument("--destination-base", required=True)

    batch_staged_parser = commands.add_parser(
        "validate-staged-batch",
        help="re-verify an entire staged migration batch without modifying it",
    )
    _add_batch_and_repositories(batch_staged_parser)
    batch_staged_parser.add_argument("--destination-base", required=True)

    render_parser = commands.add_parser("render", help="render deterministic catalog outputs")
    render_parser.add_argument("--entries-dir", type=Path, default=Path("entries"))
    render_parser.add_argument(
        "--release-metadata",
        type=Path,
        default=Path("metadata/releases.json"),
    )
    render_parser.add_argument("--readme", type=Path, default=Path("README.md"))
    render_parser.add_argument("--catalog", type=Path, default=Path("catalog.json"))
    render_parser.add_argument("--check", action="store_true")

    audit_parser = commands.add_parser(
        "audit",
        help="fetch every immutable source and destination and re-run verification",
    )
    audit_parser.add_argument("--entries-dir", type=Path, default=Path("entries"))
    audit_parser.add_argument("--specs-dir", type=Path, default=Path("specs"))

    _add_discover_parser(commands)

    census_parser = commands.add_parser(
        "census",
        help="classify every tracked file at one frozen legacy commit",
    )
    census_parser.add_argument("--policy", type=Path, required=True)
    census_parser.add_argument("--source-repo", type=Path, required=True)
    census_parser.add_argument("--output", type=Path, required=True)
    census_parser.add_argument("--check", action="store_true")

    plan_parser = commands.add_parser(
        "plan",
        help="generate exact migration specs from a frozen census and reviewed policy",
    )
    plan_parser.add_argument("--policy", type=Path, required=True)
    plan_parser.add_argument("--census", type=Path, required=True)
    plan_parser.add_argument("--output", type=Path, required=True)
    plan_parser.add_argument("--check", action="store_true")

    _add_archive_parser(commands)
    _add_manifest_parser(commands)
    _add_reconcile_parser(commands)
    _add_release_metadata_parser(commands)
    return parser


def _add_manifest_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    manifest_parser = commands.add_parser(
        "materialize-manifest",
        help="measure one immutable source payload and write its canonical manifest",
    )
    _add_spec_and_repositories(manifest_parser, destination=False)
    manifest_parser.add_argument("--output", type=Path, required=True)
    manifest_parser.add_argument("--check", action="store_true")


def _add_reconcile_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    reconcile_parser = commands.add_parser(
        "reconcile",
        help="verify generated shard manifests and write missing specs and entries",
    )
    reconcile_parser.add_argument("--shard-repo", type=Path, required=True)
    reconcile_parser.add_argument("--destination-revision", required=True)
    reconcile_parser.add_argument("--specs-dir", type=Path, default=Path("specs"))
    reconcile_parser.add_argument("--entries-dir", type=Path, default=Path("entries"))


def _add_archive_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    archive_parser = commands.add_parser(
        "render-archive",
        help="render one deterministic archive-shard README from reviewed specs",
    )
    archive_parser.add_argument("--specs-dir", type=Path, required=True)
    archive_parser.add_argument("--destination-repository", required=True)
    archive_parser.add_argument("--output", type=Path, required=True)
    archive_parser.add_argument("--check", action="store_true")


def _add_release_metadata_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    metadata_parser = commands.add_parser(
        "release-metadata",
        help="generate curated release labels from one pinned AppleDB commit",
    )
    metadata_parser.add_argument("--entries-dir", type=Path, default=Path("entries"))
    metadata_parser.add_argument("--appledb-repo", type=Path, required=True)
    metadata_parser.add_argument("--appledb-commit", required=True)
    metadata_parser.add_argument(
        "--output",
        type=Path,
        default=Path("metadata/releases.json"),
    )
    metadata_parser.add_argument("--check", action="store_true")


def _add_spec_and_repositories(parser: argparse.ArgumentParser, *, destination: bool) -> None:
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--source-repo", type=Path, required=True)
    if destination:
        parser.add_argument("--destination-repo", type=Path, required=True)


def _add_batch_and_repositories(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--spec",
        type=Path,
        action="append",
        required=True,
        help="reviewed spec path; repeat for each batch member (minimum 2)",
    )
    parser.add_argument("--source-repo", type=Path, required=True)
    parser.add_argument("--destination-repo", type=Path, required=True)


def _run_census(arguments: argparse.Namespace) -> None:
    result = census(
        arguments.policy,
        arguments.source_repo,
        arguments.output,
        check=arguments.check,
    )
    verb = "Checked" if result.checked else "Wrote"
    print(
        f"{verb} {result.output}: payloads={result.payload_count} "
        f"ordinary={result.ordinary_count} blocked={result.blocked_count} "
        f"files={result.tracked_file_count} bytes={result.logical_bytes}"
    )


def _run_plan(arguments: argparse.Namespace) -> None:
    result = plan(arguments.policy, arguments.census, arguments.output, check=arguments.check)
    verb = "Checked" if result.checked else "Wrote"
    print(f"{verb} {result.specification_count} migration specs in {result.output}")


def _run_archive(arguments: argparse.Namespace) -> None:
    count = render_archive(
        arguments.specs_dir,
        arguments.destination_repository,
        arguments.output,
        check=arguments.check,
    )
    verb = "Checked" if arguments.check else "Rendered"
    print(f"{verb} {arguments.output}: diffs={count}")


def _run_release_metadata(arguments: argparse.Namespace) -> None:
    result = import_release_metadata(
        arguments.entries_dir,
        arguments.appledb_repo,
        arguments.appledb_commit,
        arguments.output,
        check=arguments.check,
    )
    verb = "Checked" if result.checked else "Wrote"
    print(
        f"{verb} {result.output}: releases={result.release_count} "
        f"betas={result.beta_count} rcs={result.rc_count}"
    )


def _run_manifest(arguments: argparse.Namespace) -> None:
    spec = MigrationSpec.from_path(arguments.spec)
    inventory = materialize_manifest(
        spec,
        arguments.source_repo,
        arguments.output,
        check=arguments.check,
    )
    verb = "Checked" if arguments.check else "Materialized"
    print(
        f"{verb} {arguments.output}: tree={inventory.tree} "
        f"files={inventory.file_count} bytes={inventory.logical_bytes}"
    )


def _run_batch(arguments: argparse.Namespace) -> None:
    specs = tuple(MigrationSpec.from_path(path) for path in arguments.spec)
    operation = stage_batch if arguments.command == "stage-batch" else validate_staged_batch
    result = operation(
        specs,
        arguments.source_repo,
        arguments.destination_repo,
        arguments.destination_base,
    )
    verb = "Staged" if arguments.command == "stage-batch" else "Validated staged"
    print(
        f"{verb} batch: payloads={len(result.payloads)} tree={result.staged_tree} "
        f"files={result.tracked_file_count} bytes={result.logical_bytes} "
        f"paths={result.staged_path_count} base={result.base_commit}"
    )


def _run_reconcile(arguments: argparse.Namespace) -> None:
    result = reconcile(
        arguments.shard_repo,
        arguments.destination_revision,
        arguments.specs_dir,
        arguments.entries_dir,
    )
    print(
        f"Reconciled {result.reconciled_count} of {result.manifest_count} manifests "
        f"at {result.destination_commit}; recorded={result.recorded_count}"
    )


def _run(arguments: argparse.Namespace) -> None:
    if arguments.command == "audit":
        count = audit(arguments.entries_dir, arguments.specs_dir)
        print(f"Audited {count} catalog entr{'y' if count == 1 else 'ies'}")
        return
    if arguments.command == "render":
        render(
            arguments.entries_dir,
            arguments.release_metadata,
            arguments.readme,
            arguments.catalog,
            check=arguments.check,
        )
        print(
            f"{'Checked' if arguments.check else 'Rendered'} "
            f"{arguments.readme} and {arguments.catalog}"
        )
        return
    if arguments.command == "discover":
        decision = discover(
            arguments.policy,
            arguments.manifests_dir,
            arguments.appledb_repo,
            arguments.appledb_commit,
            arguments.ipsw_sources,
            ota_sources=arguments.ota_sources,
            pallas_sources=tuple(arguments.pallas_sources),
        )
        print(decision.to_json())
        return

    spec = MigrationSpec.from_path(arguments.spec)
    if arguments.command == "verify":
        result = verify(
            spec,
            arguments.source_repo,
            arguments.destination_repo,
            arguments.destination_revision,
        )
        print(
            f"Verified {spec.identifier}: tree={result.source.tree} "
            f"files={result.source.file_count} bytes={result.source.logical_bytes} "
            f"destination_commit={result.destination_commit}"
        )
        return
    if arguments.command == "record":
        path = record(
            spec,
            arguments.source_repo,
            arguments.destination_repo,
            arguments.destination_revision,
            arguments.entries_dir,
        )
        print(f"Recorded {path}")
        return
    if arguments.command in {"stage", "validate-staged"}:
        operation = stage if arguments.command == "stage" else validate_staged
        result = operation(
            spec,
            arguments.source_repo,
            arguments.destination_repo,
            arguments.destination_base,
        )
        verb = "Staged" if arguments.command == "stage" else "Validated staged"
        print(
            f"{verb} {spec.identifier}: tree={result.inventory.tree} "
            f"files={result.inventory.file_count} bytes={result.inventory.logical_bytes} "
            f"paths={result.staged_path_count} base={result.base_commit}"
        )
        return
    raise CatalogError(f"unsupported command: {arguments.command}")


def _dispatch(arguments: argparse.Namespace) -> None:
    if arguments.command == "census":
        _run_census(arguments)
    elif arguments.command == "plan":
        _run_plan(arguments)
    elif arguments.command == "render-archive":
        _run_archive(arguments)
    elif arguments.command == "release-metadata":
        _run_release_metadata(arguments)
    elif arguments.command == "materialize-manifest":
        _run_manifest(arguments)
    elif arguments.command in {"stage-batch", "validate-staged-batch"}:
        _run_batch(arguments)
    elif arguments.command == "reconcile":
        _run_reconcile(arguments)
    else:
        _run(arguments)


# Exit status for "AppleDB has not recorded a new build yet": callers retry instead of failing.
EXIT_WAIT_FOR_APPLEDB = 3


def main() -> None:
    try:
        _dispatch(_parser().parse_args())
    except AppleDBCoverageMissingError as error:
        print(f"waiting for AppleDB: {error}", file=sys.stderr)
        raise SystemExit(EXIT_WAIT_FOR_APPLEDB) from error
    except CatalogError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
