from collection_manager.constants import Tier
from collection_manager.domain import ArtistResolution, ParsedArtist
from collection_manager.duplicates import detect_duplicate_groups, suggest_resolution
from collection_manager.import_service import resolve_preview
from collection_manager.parsing import parse_text


def _artist(name: str, source_line: int) -> ParsedArtist:
    return ParsedArtist(
        source_line=source_line,
        raw_line=name,
        tier=Tier.BANGERS,
        name=name,
    )


def test_detects_supported_likely_duplicate_shapes_without_merging() -> None:
    artists = [
        _artist("Yansculpts", 1),
        _artist("YanSculpts Tutorials", 2),
        _artist("YanSculpts Advanced", 3),
        _artist("YanSculpts Free", 4),
        _artist("CGMasters", 5),
        _artist("CG Masters Advanced", 6),
        _artist("Ducky3D", 7),
        _artist("The Ducky 3D", 8),
        _artist("Unrelated Artist", 9),
    ]

    groups = detect_duplicate_groups(artists)
    member_sets = {group.member_indexes for group in groups}

    assert (0, 1, 2, 3) in member_sets
    assert (4, 5) in member_sets
    assert (6, 7) in member_sets
    assert all(8 not in group.member_indexes for group in groups)
    assert all(not group.definite for group in groups)


def test_exact_case_insensitive_duplicates_are_definite() -> None:
    group = detect_duplicate_groups([_artist("  Artist Name ", 1), _artist("artist name", 2)])[0]
    assert group.definite is True


def test_suggested_resolution_strips_variant_and_unions_review_fields() -> None:
    artists = [
        ParsedArtist(1, "CG Boost Collection", Tier.CREAM, "CG Boost Collection", tags=["style"]),
        ParsedArtist(
            2,
            "CG Boost Early Tutorials",
            Tier.WORTH_REVISITING,
            "CG Boost Early Tutorials",
            tags=["Style", "lighting"],
            notes="older material",
        ),
    ]
    group = detect_duplicate_groups(artists)[0]

    resolution = suggest_resolution(group, artists)

    assert resolution.canonical.name == "CG Boost"
    assert resolution.canonical.tags == ["style", "lighting"]
    assert resolution.canonical.notes == "older material"


def test_false_positive_can_be_kept_as_different_artists() -> None:
    preview = parse_text(
        """Cream of the Crop
Lightning Boy Studio
Bangers
Lightning Boy Advanced
Unsavable Bangers
Other One
Worth Revisiting
Other Two
Fell Off
Other Three
Boring
Other Four
"""
    )
    group = preview.duplicate_groups[0]
    suggested = suggest_resolution(group, preview.artists)
    resolution = ArtistResolution(
        member_indexes=suggested.member_indexes,
        canonical=suggested.canonical,
        different_artists=True,
    )

    resolved = resolve_preview(preview, [resolution])

    assert {item.artist.name for item in resolved} >= {
        "Lightning Boy Studio",
        "Lightning Boy Advanced",
    }
