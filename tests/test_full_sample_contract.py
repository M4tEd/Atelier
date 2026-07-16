from collections import Counter
from pathlib import Path

from collection_manager.constants import Tier
from collection_manager.parsing import preview_import

FIXTURE = Path(__file__).parent / "fixtures" / "sample_collections.txt"


def test_full_sample_parses_every_source_line() -> None:
    preview = preview_import(FIXTURE)

    assert len(preview.artists) == 208
    assert preview.unparseable == []
    assert Counter(artist.tier for artist in preview.artists) == {
        Tier.CREAM: 18,
        Tier.BANGERS: 49,
        Tier.UNSAVABLE_BANGERS: 19,
        Tier.WORTH_REVISITING: 73,
        Tier.FELL_OFF: 8,
        Tier.BORING: 41,
    }


def test_full_sample_surfaces_required_duplicate_aliases() -> None:
    preview = preview_import(FIXTURE)
    grouped_names = [
        {preview.artists[index].name for index in group.member_indexes}
        for group in preview.duplicate_groups
    ]

    assert {
        "Yansculpts",
        "YanSculpts Tutorials",
        "YanSculpts Advanced",
        "YanSculpts Free",
    } in grouped_names
    assert {
        "CGDive Collection",
        "CGDive Advanced",
        "CGDive Retired",
        "CGDive Free",
    } in grouped_names
    assert {"CGMasters", "CG Masters Advanced", "CG Masters Free"} in grouped_names


def test_full_sample_preserves_ambiguous_size_for_review() -> None:
    preview = preview_import(FIXTURE)
    jayanam = next(artist for artist in preview.artists if artist.name == "Jayanam")

    assert str(jayanam.size_value) == "4"
    assert jayanam.heavy_status.value == "unknown"
    assert any("heavy threshold" in warning for warning in jayanam.warnings)
