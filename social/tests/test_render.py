from __future__ import annotations

from pathlib import Path

from conftest import sample_entry
from PIL import Image

from ipsw_diff_social.cli import _default_base_image
from ipsw_diff_social.models import DiffFact, ReleaseNames
from ipsw_diff_social.publisher import MAX_CARD_BYTES
from ipsw_diff_social.render import CANVAS, card_alt_text, release_title, render_card

FACTS = [
    DiffFact(area="Mach-O", change="updated", count=1_297),
    DiffFact(area="filesystem", change="added", count=26),
]


def names() -> ReleaseNames:
    return ReleaseNames(
        names={
            ("iOS", "24A5418b"): "27.0 beta 6",
            ("iOS", "24A5424a"): "27.0 beta 7",
        }
    )


def test_release_title_uses_release_metadata() -> None:
    assert release_title(sample_entry(), names()) == "iOS 27.0 beta 6 → iOS 27.0 beta 7"


def test_card_from_the_brand_art_is_a_jpeg_bluesky_accepts(tmp_path: Path) -> None:
    output = tmp_path / "card.jpg"

    render_card(_default_base_image(), output, sample_entry(), names(), FACTS)

    with Image.open(output) as rendered:
        assert rendered.size == CANVAS
        assert rendered.format == "JPEG"
    assert output.stat().st_size <= MAX_CARD_BYTES


def test_alt_text_describes_the_release_builds_and_facts() -> None:
    assert card_alt_text(sample_entry(), names(), FACTS) == (
        "ipsw-diff card: iOS 27.0 beta 6 → iOS 27.0 beta 7. "
        "Builds 24A5418b to 24A5424a on iPhone18,1. "
        "1,297 Mach-O updated, 26 filesystem added."
    )
