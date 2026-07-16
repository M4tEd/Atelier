from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from collection_manager.constants import (
    CollectionKind,
    HeavyStatus,
    PointEventKind,
    SizeQualifier,
    Tier,
)
from collection_manager.database import Database
from collection_manager.domain import ArtistResolution
from collection_manager.duplicates import suggest_resolution
from collection_manager.exporting import export_text
from collection_manager.import_service import (
    ImportConflictsError,
    ImportService,
    UnresolvedDuplicatesError,
)
from collection_manager.models import Artist, ImportResolution, ImportRun, PointEvent, TierEvent
from collection_manager.parsing import parse_text, preview_import
from collection_manager.repository import ArtistRepository


def _document(cream_lines: str = "", banger_lines: str = "") -> str:
    return f"""Cream of the Crop
{cream_lines}

Bangers
{banger_lines}

Unsavable Bangers
Unique Three

Worth Revisiting
Unique Four

Fell Off
Unique Five

Boring
Unique Six
"""


@pytest.fixture
def database(tmp_path: Path) -> Database:
    result = Database(tmp_path / "library" / "collection-manager.sqlite3")
    result.initialize()
    return result


def test_unresolved_duplicate_groups_block_commit(database: Database) -> None:
    preview = parse_text(
        _document(
            "YanSculpts [2026-05-01] (3) [sculpting]",
            "YanSculpts Free [2025-01-01]",
        )
    )

    with pytest.raises(UnresolvedDuplicatesError):
        ImportService(database, CollectionKind.VIDEOS).apply_import(preview)

    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(Artist)) == 0
        assert session.scalar(select(func.count()).select_from(ImportRun)) == 0


def test_transactional_import_records_resolution_tags_and_opening_event(
    database: Database,
) -> None:
    preview = parse_text(
        _document(
            "YanSculpts [2026-05-01] (3) [sculpting]",
            "YanSculpts Free [2025-01-01] [lighting]",
        )
    )
    resolution = suggest_resolution(preview.duplicate_groups[0], preview.artists)

    result = ImportService(database, CollectionKind.VIDEOS).apply_import(preview, [resolution])

    assert result.added == 5
    assert result.backup_path is not None
    assert result.backup_path.exists()
    with database.session() as session:
        artist = session.scalar(select(Artist).where(Artist.name_key == "yansculpts"))
        assert artist is not None
        assert artist.tag_names == ["sculpting", "lighting"]
        event = session.scalar(select(PointEvent).where(PointEvent.artist_id == artist.id))
        assert event is not None
        assert event.kind == PointEventKind.LEGACY_OPENING.value
        assert (event.old_points, event.new_points) == (0, 3)
        assert event.pending_tier_shift is False  # top tier has no legal upward shift
        trace = session.scalar(
            select(ImportResolution).where(ImportResolution.artist_id == artist.id)
        )
        assert trace is not None
        assert trace.decision == "merged"
        assert len(trace.source_lines) == 2


def test_same_names_import_independently_into_video_and_image_collections(
    database: Database,
) -> None:
    preview = parse_text(_document("Shared Artist [2026-01-01] (1) [video]"))

    video_result = ImportService(database, CollectionKind.VIDEOS).apply_import(preview)
    image_result = ImportService(database, CollectionKind.IMAGES).apply_import(preview)

    assert video_result.collection_kind is CollectionKind.VIDEOS
    assert image_result.collection_kind is CollectionKind.IMAGES
    with database.session() as session:
        shared = session.scalars(
            select(Artist)
            .where(Artist.name_key == "shared artist")
            .order_by(Artist.collection_kind)
        ).all()
        assert [(artist.collection_kind, artist.points) for artist in shared] == [
            (CollectionKind.IMAGES.value, 1),
            (CollectionKind.VIDEOS.value, 1),
        ]
        runs = session.scalars(select(ImportRun).order_by(ImportRun.id)).all()
        assert [run.collection_kind for run in runs] == [
            CollectionKind.VIDEOS.value,
            CollectionKind.IMAGES.value,
        ]


def test_reimport_updates_only_the_target_collection(database: Database) -> None:
    initial = parse_text(_document("Shared Artist [2026-01-01] (1) [initial]"))
    videos = ImportService(database, CollectionKind.VIDEOS)
    images = ImportService(database, CollectionKind.IMAGES)
    videos.apply_import(initial)
    images.apply_import(initial)
    changed = parse_text(_document("Shared Artist [2026-02-02] (2) [changed]"))

    conflicts = videos.find_conflicts(changed)
    assert any(conflict.name == "Shared Artist" for conflict in conflicts)
    videos.apply_import(changed, conflict_resolutions={"Shared Artist": "import"})

    with database.session() as session:
        records = {
            artist.collection_kind: artist
            for artist in session.scalars(select(Artist).where(Artist.name_key == "shared artist"))
        }
        assert records[CollectionKind.VIDEOS.value].points == 2
        assert records[CollectionKind.VIDEOS.value].tag_names == ["changed"]
        assert records[CollectionKind.IMAGES.value].points == 1
        assert records[CollectionKind.IMAGES.value].tag_names == ["initial"]


def test_export_is_crlf_utf8_and_semantically_round_trips(
    database: Database, tmp_path: Path
) -> None:
    source = tmp_path / "source.txt"
    source.write_text(
        _document(
            'Unicode Élan [2026-01-15] (2, 5.8GB+) [lighting, "tag, comma"]',
            "Another Artist [Jul 27, 2025] (a note)",
        ),
        encoding="utf-8",
    )
    initial = preview_import(source)
    ImportService(database, CollectionKind.VIDEOS).apply_import(initial)

    destination = tmp_path / "export.txt"
    export_text(database, destination, collection_kind=CollectionKind.VIDEOS)
    raw = destination.read_bytes()

    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert "Élan" in raw.decode("utf-8")
    round_trip = preview_import(destination)
    assert not round_trip.unparseable
    assert len(round_trip.artists) == len(initial.artists)
    by_name = {artist.name: artist for artist in round_trip.artists}
    assert by_name["Unicode Élan"].points == 2
    assert by_name["Unicode Élan"].tags == ["lighting", "tag, comma"]
    assert by_name["Another Artist"].notes == "a note"


def test_reimport_requires_decision_and_logs_point_and_tier_merge(database: Database) -> None:
    service = ImportService(database, CollectionKind.VIDEOS)
    original = parse_text(_document("Changing Artist [2026-01-01] (1)", "Stable Artist"))
    service.apply_import(original)
    changed = parse_text(_document("", "Changing Artist [2026-02-02] (points: -3) [new tag]"))

    conflicts = service.find_conflicts(changed)
    changing = next(conflict for conflict in conflicts if conflict.name == "Changing Artist")
    assert {"tier", "points", "last_updated", "tags"} <= set(changing.changed_fields)
    with pytest.raises(ImportConflictsError):
        service.apply_import(changed)

    result = service.apply_import(changed, conflict_resolutions={"Changing Artist": "import"})

    assert result.updated == 1
    with database.session() as session:
        artist = session.scalar(select(Artist).where(Artist.name_key == "changing artist"))
        assert artist is not None
        assert artist.tier == Tier.BANGERS.value
        assert artist.points == -3
        merge_event = session.scalar(
            select(PointEvent)
            .where(PointEvent.artist_id == artist.id)
            .where(PointEvent.kind == PointEventKind.LEGACY_MERGE.value)
        )
        assert merge_event is not None
        assert (merge_event.old_points, merge_event.new_points) == (1, -3)
        assert (
            session.scalar(
                select(func.count()).select_from(TierEvent).where(TierEvent.artist_id == artist.id)
            )
            == 1
        )
        # Artists omitted from a later text file are retained.
        assert session.scalar(select(Artist).where(Artist.name_key == "stable artist")) is not None


def test_different_artists_resolution_does_not_silently_merge(database: Database) -> None:
    preview = parse_text(_document("Lightning Boy Studio", "Lightning Boy Advanced"))
    suggestion = suggest_resolution(preview.duplicate_groups[0], preview.artists)
    resolution = ArtistResolution(
        member_indexes=suggestion.member_indexes,
        canonical=replace(suggestion.canonical),
        different_artists=True,
    )

    result = ImportService(database, CollectionKind.VIDEOS).apply_import(preview, [resolution])

    assert result.added == 6
    with database.session() as session:
        names = set(session.scalars(select(Artist.name)))
        assert {"Lightning Boy Studio", "Lightning Boy Advanced"} <= names


def test_canonical_export_escapes_artist_name_and_reserved_single_tags(
    database: Database, tmp_path: Path
) -> None:
    with database.session() as session:
        ArtistRepository(session).create(
            'Foo (Studio) [Draft] "One"',
            tier=Tier.CREAM,
            tags=["2026-01-15", "https://example.test", "Vol. 1"],
        )

    destination = export_text(
        database,
        tmp_path / "reserved.txt",
        collection_kind=CollectionKind.VIDEOS,
    )
    exported = destination.read_text(encoding="utf-8")
    round_trip = preview_import(destination)

    assert exported.splitlines()[1].startswith('"Foo (Studio) [Draft] \\"One\\""')
    assert round_trip.unparseable == []
    artist = next(item for item in round_trip.artists if item.name.startswith("Foo"))
    assert artist.name == 'Foo (Studio) [Draft] "One"'
    assert artist.tags == ["2026-01-15", "https://example.test", "Vol. 1"]
    assert artist.last_updated is None
    assert artist.reference_url is None
    assert artist.notes == ""


def test_export_contains_only_the_selected_collection(database: Database, tmp_path: Path) -> None:
    with database.session() as session:
        repository = ArtistRepository(session)
        repository.create(
            "Shared Artist",
            collection_kind=CollectionKind.VIDEOS,
            tier=Tier.BANGERS,
            points=1,
            tags=["video"],
        )
        repository.create(
            "Shared Artist",
            collection_kind=CollectionKind.IMAGES,
            tier=Tier.CREAM,
            points=2,
            tags=["image"],
        )
        repository.create("Video Only", collection_kind=CollectionKind.VIDEOS)
        repository.create("Image Only", collection_kind=CollectionKind.IMAGES)

    videos_path = export_text(
        database,
        tmp_path / "videos.txt",
        collection_kind=CollectionKind.VIDEOS,
    )
    images_path = export_text(
        database,
        tmp_path / "images.txt",
        collection_kind=CollectionKind.IMAGES,
    )
    videos_text = videos_path.read_text(encoding="utf-8")
    images_text = images_path.read_text(encoding="utf-8")

    assert "Video Only" in videos_text
    assert "Image Only" not in videos_text
    assert "[video]" in videos_text
    assert "Image Only" in images_text
    assert "Video Only" not in images_text
    assert "[image]" in images_text
    assert not preview_import(videos_path).unparseable
    assert not preview_import(images_path).unparseable


def test_zero_byte_folder_size_exports_and_reimports(database: Database, tmp_path: Path) -> None:
    with database.session() as session:
        ArtistRepository(session).create(
            "Empty Folder",
            size_value=Decimal(0),
            size_unit="B",
            size_qualifier=SizeQualifier.EXACT,
        )

    destination = export_text(
        database,
        tmp_path / "empty-folder.txt",
        collection_kind=CollectionKind.VIDEOS,
    )
    preview = preview_import(destination)
    exported = next(artist for artist in preview.artists if artist.name == "Empty Folder")

    assert not preview.unparseable
    assert exported.size_value == Decimal(0)
    assert exported.size_unit == "B"
    assert exported.size_qualifier is SizeQualifier.EXACT
    assert exported.heavy_status is HeavyStatus.NO
