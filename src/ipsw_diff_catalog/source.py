from __future__ import annotations

import re
from dataclasses import dataclass

from ipsw_diff_catalog.model import CatalogError, Release

_TITLE = re.compile(
    r"# (?P<previous_version>[0-9]+(?:\.[0-9]+)*(?: [A-Za-z0-9]+)*) "
    r"\((?P<previous_build>[A-Za-z0-9]+)\) \.vs "
    r"(?P<next_version>[0-9]+(?:\.[0-9]+)*(?: [A-Za-z0-9]+)*) "
    r"\((?P<next_build>[A-Za-z0-9]+)\)"
)
_INPUT_BULLET = re.compile(r"- `([^`]+)`")
_INPUT_HEADINGS = {
    "## IPSWs": "IPSWs",
    "## Inputs": "Inputs",
}
_REDIRECT_PREFIX = "# ⚠️ Please see corrected "
_INPUT_COUNT = 2
# Discovery names full-OTA inputs `<device>_<version>_<build>_OTA.zip`.
_INPUT_SUFFIXES = (".ipsw", "_OTA.zip")


class SourceReadmeError(CatalogError):
    """A source report that cannot satisfy the ordinary two-input contract."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SourceReadme:
    input_section: str
    previous: Release
    next: Release

    def to_object(self) -> dict[str, object]:
        return {
            "input_section": self.input_section,
            "from": self.previous.to_object(),
            "to": self.next.to_object(),
        }


def _parse_title(line: str) -> re.Match[str]:
    if line.startswith(_REDIRECT_PREFIX):
        raise SourceReadmeError("redirect-readme", "source report redirects to another diff")
    title = _TITLE.fullmatch(line)
    if title is None:
        raise SourceReadmeError(
            "unsupported-readme",
            f"source report has an unsupported title: {line!r}",
        )
    return title


def _parse_inputs(lines: list[str]) -> tuple[str, tuple[str, str]]:
    headings = [
        (index, _INPUT_HEADINGS[line])
        for index, line in enumerate(lines)
        if line in _INPUT_HEADINGS
    ]
    if len(headings) != 1:
        raise SourceReadmeError(
            "unsupported-readme",
            "source report must have exactly one '## Inputs' or '## IPSWs' section; "
            f"found {len(headings)}",
        )

    start, label = headings[0]
    section: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line:
            section.append(line)
    if len(section) != _INPUT_COUNT:
        raise SourceReadmeError(
            "unsupported-readme",
            "source report inputs differ: the input section must contain exactly two bullets",
        )

    input_names: list[str] = []
    for line in section:
        match = _INPUT_BULLET.fullmatch(line)
        if match is None:
            raise SourceReadmeError(
                "unsupported-readme",
                "source report inputs differ: each input must be one exact code-formatted bullet",
            )
        input_names.append(match.group(1))
    suffixes = [
        next((suffix for suffix in _INPUT_SUFFIXES if name.endswith(suffix)), None)
        for name in input_names
    ]
    if None in suffixes:
        raise SourceReadmeError(
            "non-ipsw-inputs",
            "source report input section contains a non-IPSW artifact",
        )
    if suffixes[0] != suffixes[1]:
        raise SourceReadmeError(
            "mixed-inputs",
            "source report inputs differ in kind: diff two IPSWs or two full OTAs",
        )
    return label, (input_names[0], input_names[1])


def _release(version: str, build: str, input_name: str, context: str) -> Release:
    try:
        return Release.from_object(
            {"version": version, "build": build, "input": input_name},
            context,
        )
    except CatalogError as error:
        raise SourceReadmeError(
            "unsupported-readme",
            f"source report release metadata is invalid: {error}",
        ) from error


def parse_source_readme(readme: str) -> SourceReadme:
    lines = readme.splitlines()
    if not lines:
        raise SourceReadmeError("unsupported-readme", "source report is empty")
    title = _parse_title(lines[0])
    label, input_names = _parse_inputs(lines)

    previous = _release(
        title.group("previous_version"),
        title.group("previous_build"),
        input_names[0],
        "source report from",
    )
    next_release = _release(
        title.group("next_version"),
        title.group("next_build"),
        input_names[1],
        "source report to",
    )
    return SourceReadme(input_section=label, previous=previous, next=next_release)
