from __future__ import annotations

import pytest
import requests
from conftest import entry_document

from ipsw_diff_social import catalog
from ipsw_diff_social.catalog import fetch_catalog, parse_diff_facts
from ipsw_diff_social.models import CatalogEntry, DataError


@pytest.mark.parametrize("version", [None, 0, 2, "1", True, 1.0])
@pytest.mark.parametrize("scope", ["catalog", "entry"])
def test_catalog_rejects_missing_or_unsupported_schema_versions(
    version: object, scope: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = entry_document()
    document: dict[str, object] = {"schema_version": 1, "entries": [row]}
    target = document if scope == "catalog" else row
    if version is None:
        del target["schema_version"]
    else:
        target["schema_version"] = version
    monkeypatch.setattr(catalog, "fetch_json", lambda *_args: document)

    with pytest.raises(DataError, match=rf"{scope}\.schema_version"):
        fetch_catalog(requests.Session(), "a" * 40)


@pytest.mark.parametrize(
    "document",
    [
        [],
        {},
        {"schema_version": 1},
        {"schema_version": 1, "entries": {}},
        {"schema_version": 1, "entries": [None]},
    ],
)
def test_catalog_rejects_malformed_documents(
    document: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog, "fetch_json", lambda *_args: document)

    with pytest.raises(DataError):
        fetch_catalog(requests.Session(), "a" * 40)


def test_catalog_accepts_version_one(monkeypatch: pytest.MonkeyPatch) -> None:
    row = entry_document()
    monkeypatch.setattr(
        catalog, "fetch_json", lambda *_args: {"schema_version": 1, "entries": [row]}
    )

    assert fetch_catalog(requests.Session(), "a" * 40) == [CatalogEntry.from_value(row)]


def test_catalog_entry_canonicalizes_legacy_org_and_builds_immutable_urls() -> None:
    entry = CatalogEntry.from_value(entry_document())

    assert entry.destination.page_url == (
        f"https://github.com/ipsw-diffs/ios-27/blob/{'a' * 40}/diffs/example/README.md"
    )
    assert entry.destination.raw_url == (
        f"https://raw.githubusercontent.com/ipsw-diffs/ios-27/{'a' * 40}/diffs/example/README.md"
    )


def test_catalog_entry_rejects_path_traversal() -> None:
    value = entry_document()
    destination = value["destination"]
    assert isinstance(destination, dict)
    destination["entrypoint"] = "../README.md"

    with pytest.raises(DataError, match="safe relative path"):
        CatalogEntry.from_value(value)


def test_catalog_entry_rejects_destination_outside_publishing_org() -> None:
    value = entry_document()
    destination = value["destination"]
    assert isinstance(destination, dict)
    destination["repository"] = "https://github.com/example/ios-27"

    with pytest.raises(DataError, match="ipsw-diffs organization"):
        CatalogEntry.from_value(value)


def test_catalog_entry_accepts_comma_safe_device_suffix() -> None:
    value = entry_document()
    value["id"] = "ios-18.4-build-build-iPhone17,5"

    assert CatalogEntry.from_value(value).entry_id.endswith("iPhone17,5")


def test_parse_diff_facts_uses_largest_counted_sections() -> None:
    markdown = """
## MachO
### filesystem
#### ⬆️ Updated (97)
## Firmware
### ⬆️ Updated (3)
## DSC
### Dylibs
#### ⬆️ Updated (26)
## Files
### 🆕 New
#### filesystem (2)
"""

    facts = parse_diff_facts(markdown)

    assert [(fact.area, fact.change, fact.count) for fact in facts] == [
        ("filesystem", "updated", 97),
        ("Dylibs", "updated", 26),
        ("Firmware", "updated", 3),
    ]


def test_parse_diff_facts_uses_parent_change_and_counted_area() -> None:
    markdown = """
## Files
### 🆕 New
#### filesystem (6)
### ❌ Removed
#### filesystem (5)
"""

    facts = parse_diff_facts(markdown)

    assert [(fact.area, fact.change, fact.count) for fact in facts] == [
        ("filesystem", "added", 6),
        ("filesystem", "removed", 5),
    ]


def test_parse_diff_facts_ignores_heading_shaped_lines_in_fences() -> None:
    markdown = """
## MachO
### filesystem
#### ⬆️ Updated (7)

```markdown
#### __TEXT.__const (12,345)
```

~~~text
#### fake (9,999)
~~~
"""

    facts = parse_diff_facts(markdown)

    assert [(fact.area, fact.change, fact.count) for fact in facts] == [
        ("filesystem", "updated", 7)
    ]
