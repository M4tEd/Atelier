from datetime import date
from decimal import Decimal

from collection_manager.constants import HeavyStatus, SizeQualifier, Tier
from collection_manager.parsing import infer_heavy_status, parse_artist_line, parse_text


def test_parses_legacy_mixed_metadata_and_named_dates() -> None:
    artist = parse_artist_line(
        "Ponte Ryuurui [Aug 19, 2025] (2, look for older courses) [environments, lighting]",
        Tier.CREAM,
        12,
    )

    assert artist.source_line == 12
    assert artist.last_updated == date(2025, 8, 19)
    assert artist.points == 2
    assert artist.notes == "look for older courses"
    assert artist.tags == ["environments", "lighting"]


def test_parses_sizes_and_keeps_ambiguous_lower_bound_for_review() -> None:
    heavy = parse_artist_line("CG Boost (5.8GB+)", Tier.BANGERS)
    ambiguous = parse_artist_line("Jayanam (2, 4+ GB)", Tier.BANGERS)

    assert heavy.size_value == Decimal("5.8")
    assert heavy.size_unit == "GB"
    assert heavy.size_qualifier is SizeQualifier.AT_LEAST
    assert heavy.heavy_status is HeavyStatus.YES
    assert ambiguous.points == 2
    assert ambiguous.size_value == Decimal(4)
    assert ambiguous.heavy_status is HeavyStatus.UNKNOWN
    assert any("threshold" in warning for warning in ambiguous.warnings)


def test_byte_sizes_and_binary_units_use_a_decimal_gb_threshold() -> None:
    tiny = parse_artist_line("Tiny Folder (size: 999 B)", Tier.BANGERS)

    assert tiny.size_value == Decimal(999)
    assert tiny.size_unit == "B"
    assert tiny.size_qualifier is SizeQualifier.EXACT
    assert tiny.heavy_status is HeavyStatus.NO
    assert infer_heavy_status(Decimal("4.7"), "GiB", SizeQualifier.EXACT) is HeavyStatus.YES
    assert infer_heavy_status(Decimal("4.6"), "GiB", SizeQualifier.EXACT) is HeavyStatus.NO


def test_url_volume_note_empty_group_and_heavy_note() -> None:
    url = parse_artist_line("Arrimus3D [https://www.youtube.com/@Arrimus3D]", Tier.BANGERS)
    volume = parse_artist_line("CG Cookie Fundamentals [Vol. 1 - 3]", Tier.BANGERS)
    empty = parse_artist_line("Erindale () [low poly]", Tier.BANGERS)
    warning = parse_artist_line(
        "YanSculpts Advanced (heavy files, download compressed)", Tier.WORTH_REVISITING
    )

    assert url.reference_url == "https://www.youtube.com/@Arrimus3D"
    assert volume.notes == "Vol. 1 - 3"
    assert volume.tags == []
    assert empty.tags == ["low poly"]
    assert warning.heavy_status is HeavyStatus.UNKNOWN
    assert warning.is_compressed is False
    assert any("confirm heavy status" in item for item in warning.warnings)


def test_parses_canonical_json_strings_and_explicit_flags() -> None:
    artist = parse_artist_line(
        "Artist [2026-01-15] (points: -2) (size: ~10 GB) "
        "(heavy: yes) (compressed: true) "
        '(note: "contains ) and comma, plus \\"quotes\\"") '
        '(url: "https://example.test/a(b)") [lighting, "tag, with comma"]',
        Tier.CREAM,
    )

    assert artist.points == -2
    assert artist.size_qualifier is SizeQualifier.APPROXIMATE
    assert artist.heavy_status is HeavyStatus.YES
    assert artist.is_compressed is True
    assert artist.notes == 'contains ) and comma, plus "quotes"'
    assert artist.reference_url == "https://example.test/a(b)"
    assert artist.tags == ["lighting", "tag, with comma"]


def test_complete_document_tracks_bad_lines_without_aborting() -> None:
    text = """Cream of the Crop
Good Artist [2026-01-01]
Broken Artist [2026-01-01

Bangers
Another Artist

Unsavable Bangers
Third Artist

Worth Revisiting
Fourth Artist

Fell off
Fifth Artist

Boring
Sixth Artist
"""
    preview = parse_text(text)

    assert len(preview.artists) == 6
    assert preview.unparseable[0][0] == 3
    assert "Unclosed" in preview.unparseable[0][2]
    assert {artist.tier for artist in preview.artists} == set(Tier)
