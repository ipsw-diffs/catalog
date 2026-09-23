from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ipsw_diff_catalog.model import (
    CatalogEntry,
    CatalogError,
    JsonObject,
    canonical_json,
    read_json_object,
)
from ipsw_diff_catalog.release_registry import ReleaseKey, ReleaseLabel, load_release_labels

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from ipsw_diff_catalog.model import Release

_APPLE_BUILD = re.compile(r"(\d+)([A-Z])(\d+)([A-Za-z]*)")
_APPLE_BETA_BUILD_FLOOR = 1000
_LATEST_PER_PLATFORM = 3
_TABLE_HEADER = (
    "| Device | Comparison | Integrity |",
    "| --- | --- | --- |",
)


def load_entries(entries_dir: Path) -> tuple[CatalogEntry, ...]:
    paths = sorted(entries_dir.glob("*.json"))
    if not paths:
        raise CatalogError(f"no catalog entries found in {entries_dir}")
    loaded: list[CatalogEntry] = []
    for path in paths:
        entry = CatalogEntry.from_object(read_json_object(path))
        if path.stem != entry.identifier:
            raise CatalogError(
                f"catalog entry filename must match id: {path.name} != {entry.identifier}.json"
            )
        loaded.append(entry)
    entries = tuple(loaded)
    _require_unique(entries, "id", lambda entry: entry.identifier)
    _require_unique(
        entries,
        "destination repository/path",
        lambda entry: f"{entry.destination_repository}:{entry.payload_path}",
    )
    _require_unique(
        entries,
        "source commit/path",
        lambda entry: f"{entry.source.commit}:{entry.source.path}",
    )
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                entry.platform.casefold(),
                -entry.major_version,
                entry.next.version,
                entry.next.build,
                entry.previous.version,
                entry.previous.build,
                entry.identifier,
            ),
        )
    )


def _require_unique(
    entries: Iterable[CatalogEntry],
    label: str,
    key: Callable[[CatalogEntry], str],
) -> None:
    seen: set[str] = set()
    for entry in entries:
        value = key(entry)
        if value in seen:
            raise CatalogError(f"duplicate catalog {label}: {value}")
        seen.add(value)


def catalog_object(entries: tuple[CatalogEntry, ...]) -> JsonObject:
    return {"schema_version": 1, "entries": [entry.data for entry in entries]}


def _version_key(version: str) -> tuple[int, ...]:
    numeric = version.split(" ", maxsplit=1)[0]
    return tuple(int(part) for part in numeric.split("."))


def _build_key(build: str) -> tuple[int, str, int, int, str]:
    match = _APPLE_BUILD.fullmatch(build)
    if match is None:
        return (0, "", 0, 0, build)
    os_version, train, number, suffix = match.groups()
    numeric = int(number)
    release_rank = 0 if numeric >= _APPLE_BETA_BUILD_FLOOR else 1
    return (int(os_version), train, release_rank, numeric, suffix)


def _release_key(entry: CatalogEntry) -> tuple[object, ...]:
    return (
        _version_key(entry.next.version),
        _build_key(entry.next.build),
        _version_key(entry.previous.version),
        _build_key(entry.previous.build),
        entry.identifier,
    )


def _release_text(
    platform: str,
    release: Release,
    labels: dict[ReleaseKey, ReleaseLabel],
    *,
    code_build: bool,
) -> str:
    label = labels.get((platform, release.build))
    if label is None:
        raise CatalogError(f"release label is missing: {platform} {release.build}")
    build = f"`{release.build}`" if code_build else release.build
    return f"{label.display_version} ({build})"


def _append_table(
    lines: list[str],
    entries: list[CatalogEntry],
    labels: dict[ReleaseKey, ReleaseLabel],
) -> None:
    lines.extend(_TABLE_HEADER)
    for entry in sorted(entries, key=_release_key, reverse=True):
        manifest = (
            f"[manifest]({entry.destination_repository}/blob/"
            f"{entry.destination_commit}/{entry.manifest_path})"
        )
        integrity = (
            f"`{entry.inventory.tree[:12]}` · {entry.inventory.file_count:,} files · "
            f"{entry.inventory.logical_bytes:,} bytes · {manifest}"
        )
        lines.append(f"| `{entry.device}` | {_comparison_link(entry, labels)} | {integrity} |")


def _comparison_link(
    entry: CatalogEntry,
    labels: dict[ReleaseKey, ReleaseLabel],
) -> str:
    previous = _release_text(entry.platform, entry.previous, labels, code_build=False)
    following = _release_text(entry.platform, entry.next, labels, code_build=False)
    return (
        f"[{previous} → {following}]"
        f"({entry.destination_repository}/blob/"
        f"{entry.destination_commit}/{entry.entrypoint})"
    )


def _latest_lines(
    entries: list[CatalogEntry],
    labels: dict[ReleaseKey, ReleaseLabel],
) -> list[str]:
    if not entries:
        return ["_No diffs indexed._"]
    newest = sorted(entries, key=_release_key, reverse=True)[:_LATEST_PER_PLATFORM]
    lines: list[str] = []
    for entry in newest:
        previous = _release_text(entry.platform, entry.previous, labels, code_build=True)
        following = _release_text(entry.platform, entry.next, labels, code_build=True)
        lines.append(
            f"- [{following}]({entry.destination_repository}/blob/"
            f"{entry.destination_commit}/{entry.entrypoint}) ← {previous}"
        )
    return lines


def render_readme(
    entries: tuple[CatalogEntry, ...],
    labels: dict[ReleaseKey, ReleaseLabel],
) -> str:
    lines = [
        "# ipsw-diff catalog",
        "",
        "A small, machine-verified index of browsable Apple firmware-diff shards.",
        "The legacy corpus remains authoritative until each diff passes the same",
        "source-to-default-branch verification recorded here.",
        "",
        "<!-- Generated by `ipsw-diff-catalog render`; do not edit the indexes manually. -->",
        "",
        "## Latest diffs",
        "",
        "Up to three newest diffs per platform, sorted newest-first.",
        "Full comparison and integrity details are in the version browser below.",
    ]

    grouped: dict[tuple[str, int], list[CatalogEntry]] = {}
    for entry in entries:
        grouped.setdefault((entry.platform, entry.major_version), []).append(entry)

    platforms = ["iOS", "macOS"]
    latest_major: dict[str, int | None] = {}
    latest_entries: dict[str, list[CatalogEntry]] = {}
    for platform in platforms:
        majors = [major for candidate, major in grouped if candidate == platform]
        major = max(majors) if majors else None
        latest_major[platform] = major
        latest_entries[platform] = grouped[(platform, major)] if major is not None else []
    for platform in platforms:
        major = latest_major[platform]
        label = f"{platform} {major}" if major is not None else platform
        lines.extend(["", f"### {label}", "", *_latest_lines(latest_entries[platform], labels)])
    lines.extend(["", "## Browse all diffs", ""])
    for platform in platforms:
        platform_groups = sorted(
            (
                (major, group_entries)
                for (candidate, major), group_entries in grouped.items()
                if candidate == platform
            ),
            key=lambda group: group[0],
            reverse=True,
        )
        if not platform_groups:
            continue
        lines.extend([f"### {platform}", ""])
        for major, group_entries in platform_groups:
            diff_label = "diff" if len(group_entries) == 1 else "diffs"
            group_name = f"{platform} {major}"
            diff_count = f"{len(group_entries)} {diff_label}"
            summary = f"<summary><strong>{group_name}</strong> · {diff_count}</summary>"
            lines.extend(["<details>", summary, ""])
            _append_table(lines, group_entries, labels)
            lines.extend(["", "</details>", ""])
    lines.extend(
        [
            "## Integrity model",
            "",
            "Every entry is created only after a fresh destination commit matches its frozen",
            "source subtree's Git tree ID, file count, logical byte total, modes, report",
            "metadata, and generated manifest. Catalog links are pinned to immutable commits.",
            "",
            "The catalog tool never selects a diff, infers platform semantics from a path,",
            "pushes, merges, deletes legacy data, or rewrites a verified payload.",
            "",
            "## Frozen migration census",
            "",
            "`census` inventories the legacy repository from one full commit, not its",
            "worktree. The reviewed [source layout policy](migration/source-layout.json)",
            "names every payload root and every intentional exclusion. The command rejects",
            "missing or overlapping policy paths and any tracked file classified zero or",
            "multiple times. It records Git object IDs, file counts, logical bytes, modes,",
            "the selected root entrypoint, report metadata, and explicit blocker reasons in",
            "the generated [census](migration/census.json).",
            "",
            "An ordinary payload must have a root `README.md` or root `TOC.md`; `README.md`",
            "takes precedence if both exist. Both known input labels (`## Inputs` and",
            "`## IPSWs`)",
            "must satisfy the same exact title and two-IPSW contract. No nested or inferred",
            "entrypoint is accepted. A census row does not select a platform,",
            "device, destination shard, or migration route; those remain reviewed spec data.",
            "",
            "`plan` combines a frozen census with an explicit reviewed policy. The policy",
            "contains the exact source-path allowlist and input-device-prefix routes. The",
            "command fails closed unless the complete ordinary destination-major set equals",
            "the reviewed selected plus explicitly excluded paths. Planned rows require both",
            "IPSW inputs to name the same device and exactly one route; reviewed device-specific",
            "identifier suffixes preserve otherwise-colliding build pairs. The command writes",
            "deterministic specs and never copies, commits, pushes, or deletes payloads.",
            "The reviewed plan remains below `migration/` for",
            "deterministic planner checks; after destination verification, published copies",
            "move to `specs/` beside their matching catalog entries.",
            "",
            "`render-archive` selects one exact destination repository from reviewed specs",
            "and writes or checks its deterministic shard README. Rows are grouped by device",
            "and source path without claiming that branched historical data is one release",
            "sequence. Specs may name multiple immutable source repositories, but must share",
            "one platform, major version, and destination repository. It does not inspect a",
            "directory name to infer a route.",
            "",
            "## Mechanical staging",
            "",
            "`stage` requires one reviewed spec, both local repository roots, and the full",
            "destination `HEAD` commit. It refuses dirty or pre-existing targets, reconstructs",
            "the source tree twice, and leaves only the payload and manifest staged for review.",
            "Run `validate-staged` again after inspection. Neither command commits or pushes.",
            "",
            "`stage-batch` accepts two or more explicit specs that share one frozen source",
            "commit and destination shard. It verifies every member from one staged Git tree",
            "and rolls back all copier-owned batch paths if any member fails. Run",
            "`validate-staged-batch` after inspection; neither command selects or routes rows.",
            "Rollback covers handled in-process failures, not abrupt process or host termination.",
            "",
            "`materialize-manifest` supports a directly generated shard payload after the",
            "payload alone has been committed. It requires a full immutable source commit in",
            "the reviewed spec, re-parses the report, measures the committed Git tree, and",
            "writes the canonical manifest without changing the payload. It refuses to",
            "overwrite a differing manifest; `--check` re-measures the source and requires",
            "the existing output to match. It does not commit, push, update a track, or open",
            "a pull request.",
            "",
            "## Ordered read-only discovery",
            "",
            "`discover` reads one schema-v2 iOS, iPadOS-routed iOS, or macOS track policy,",
            "requires its immutable activation anchor to be a merged manifest endpoint, and",
            "reads release facts",
            "from one exact AppleDB Git commit. A separately captured `ipsw dl appledb`",
            "inventory must confirm the active Apple URL, size, and SHA-256 for every selected",
            "build. The command treats merged manifests as per-version-train heads, so a",
            "maintenance release and a newer beta train never become an accidental cross-train",
            "comparison. It emits canonical `current` or `candidate` JSON containing every",
            "missing consecutive edge after the anchor.",
            "It fails rather than guessing when dates, sources, or existing publication state",
            "are ambiguous. It does not download firmware, modify a repository, schedule work,",
            "generate a diff, open a pull request, or publish anything.",
            "",
            "## Deterministic catalog reconciliation",
            "",
            "`reconcile` accepts one local shard repository and one full merged commit SHA.",
            "It classifies every canonical manifest at that commit against the checked-in",
            "entry IDs. Recorded legacy manifests remain owned by the catalog audit; each",
            "missing generated manifest must have matching workflow provenance and an",
            "immutable source tag. The command then reuses the source/destination verifier",
            "and preflights every canonical spec/entry output before writing any of them.",
            "",
            "The caller owns fetching and commit selection. The command does not choose a",
            "branch, clone, fetch, regenerate release labels or indexes, commit, push, open",
            "a pull request, or publish anything.",
            "",
            "## Curated release metadata",
            "",
            "`release-metadata` enumerates every unique `(platform, build)` endpoint in the",
            "validated catalog, then requires one exact record below the device-selected",
            "AppleDB root at a caller-supplied Git commit. iPad devices select",
            "`osFiles/iPadOS`; other iOS devices select `osFiles/iOS`; macOS selects",
            "`osFiles/macOS`. The registry retains catalog platform `iOS` for iPad rows.",
            "It records AppleDB's",
            "human-curated display version, beta/RC flags, release date, and exact source",
            "path in [the release registry](metadata/releases.json).",
            "",
            "The importer reads immutable Git objects rather than the AppleDB worktree. It",
            "fails closed on missing or ambiguous builds, platform/build/version conflicts,",
            "invalid dates and flags, a substituted Git remote, or stale output. It does not",
            "infer beta ordinals from build numbers or change catalog payload facts.",
            "The generated README requires exact registry coverage and displays each",
            "endpoint's curated label; `catalog.json` remains payload-derived and unchanged.",
            "",
            "## Tooling",
            "",
            "```console",
            "uv sync --locked --all-groups",
            "uv run ipsw-diff-catalog --help",
            "uv run ipsw-diff-catalog stage --help",
            "uv run ipsw-diff-catalog validate-staged --help",
            "uv run ipsw-diff-catalog stage-batch --help",
            "uv run ipsw-diff-catalog validate-staged-batch --help",
            "uv run ipsw-diff-catalog materialize-manifest --help",
            "uv run ipsw-diff-catalog census --help",
            "uv run ipsw-diff-catalog plan --help",
            "uv run ipsw-diff-catalog render-archive --help",
            "uv run ipsw-diff-catalog discover --help",
            "uv run ipsw-diff-catalog reconcile --help",
            "uv run ipsw-diff-catalog release-metadata --help",
            "uv run pytest",
            "uv run ruff format --check .",
            "uv run ruff check .",
            "uv run ty check src tests",
            "uv run ipsw-diff-catalog render --check",
            "uv run ipsw-diff-catalog audit",
            "uv run ipsw-diff-catalog census \\",
            "  --policy migration/source-layout.json \\",
            "  --source-repo /path/to/ipsw-diffs \\",
            "  --output migration/census.json \\",
            "  --check",
            "uv run ipsw-diff-catalog plan \\",
            "  --policy migration/major-15.json \\",
            "  --census migration/census.json \\",
            "  --output migration/specs-15 \\",
            "  --check",
            "uv run ipsw-diff-catalog plan \\",
            "  --policy migration/major-17.json \\",
            "  --census migration/census.json \\",
            "  --output migration/specs-17 \\",
            "  --check",
            "uv run ipsw-diff-catalog plan \\",
            "  --policy migration/major-18.json \\",
            "  --census migration/census.json \\",
            "  --output migration/specs-18 \\",
            "  --check",
            "uv run ipsw-diff-catalog plan \\",
            "  --policy migration/major-26.json \\",
            "  --census migration/census.json \\",
            "  --output migration/specs-26 \\",
            "  --check",
            "uv run ipsw-diff-catalog reconcile \\",
            "  --shard-repo /path/to/shard \\",
            "  --destination-revision FULL_MERGE_SHA \\",
            "  --specs-dir specs \\",
            "  --entries-dir entries",
            "uv run ipsw-diff-catalog release-metadata \\",
            "  --appledb-repo /path/to/appledb \\",
            "  --appledb-commit ff4db9a3836c567087dc7f2efda2b27877664ebb \\",
            "  --output metadata/releases.json \\",
            "  --check",
            "```",
            "",
            "[Automation](docs/AUTOMATION.md) defines the separately gated migration,",
            "shard-generation, catalog-publication, and social-announcement transitions.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_or_check(path: Path, content: str, *, check: bool) -> None:
    if check:
        try:
            observed = path.read_text(encoding="utf-8")
        except OSError as error:
            raise CatalogError(f"cannot check generated file {path}: {error}") from error
        if observed != content:
            raise CatalogError(f"generated file is stale: {path}")
        return
    path.write_text(content, encoding="utf-8")


def render(
    entries_dir: Path,
    release_metadata: Path,
    readme: Path,
    catalog: Path,
    *,
    check: bool,
) -> None:
    entries = load_entries(entries_dir)
    labels = load_release_labels(release_metadata, entries)
    _write_or_check(readme, render_readme(entries, labels), check=check)
    _write_or_check(catalog, canonical_json(catalog_object(entries)), check=check)
