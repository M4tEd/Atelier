from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from collection_manager.constants import CollectionKind, HeavyStatus, Tier
from collection_manager.models import Artist, Base, PointEvent, RuleEffect
from collection_manager.rating_service import RatingService
from collection_manager.repository import (
    ArtistNotFoundError,
    ArtistRepository,
    DuplicateArtistError,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False, autoflush=False) as value:
        yield value


def test_create_normalizes_unique_name_and_ordered_distinct_tags(session: Session) -> None:
    repository = ArtistRepository(session)

    artist = repository.create(
        "  The   Artist  ",
        tier=Tier.BANGERS,
        tags=["Sculpting", " anatomy ", "SCULPTING", ""],
        notes="Excellent forms",
    )

    assert artist.name == "The Artist"
    assert artist.name_key == "the artist"
    assert artist.tier_value is Tier.BANGERS
    assert artist.tag_names == ["Sculpting", "anatomy"]
    assert repository.find_by_name(" THE ARTIST ") is artist

    with pytest.raises(DuplicateArtistError):
        repository.create("the artist")


def test_artist_names_are_unique_per_collection(session: Session) -> None:
    repository = ArtistRepository(session)

    video = repository.create(
        "Shared Artist",
        collection_kind=CollectionKind.VIDEOS,
        tier=Tier.BANGERS,
    )
    image = repository.create(
        " shared   artist ",
        collection_kind=CollectionKind.IMAGES,
        tier=Tier.CREAM,
    )

    assert video.collection_kind_value is CollectionKind.VIDEOS
    assert image.collection_kind_value is CollectionKind.IMAGES
    assert repository.find_by_name("SHARED ARTIST", collection_kind=CollectionKind.VIDEOS) is video
    assert repository.find_by_name("SHARED ARTIST", collection_kind=CollectionKind.IMAGES) is image
    assert repository.list(collection_kind=CollectionKind.VIDEOS) == [video]
    assert repository.list(collection_kind=CollectionKind.IMAGES) == [image]

    with pytest.raises(DuplicateArtistError):
        repository.create("Shared Artist", collection_kind=CollectionKind.VIDEOS)


def test_rename_collision_is_scoped_to_collection(session: Session) -> None:
    repository = ArtistRepository(session)
    repository.create("Video Only", collection_kind=CollectionKind.VIDEOS)
    image = repository.create("Image Original", collection_kind=CollectionKind.IMAGES)
    repository.create("Image Existing", collection_kind=CollectionKind.IMAGES)

    renamed = repository.update(image.id, name="Video Only")
    assert renamed.name == "Video Only"

    with pytest.raises(DuplicateArtistError):
        repository.update(image.id, name="Image Existing")


def test_search_filter_sort_and_metadata_update(session: Session) -> None:
    repository = ArtistRepository(session)
    alpha = repository.create(
        "Alpha",
        tier=Tier.CREAM,
        points=2,
        tags=["Blender", "Stylized"],
        notes="Great anatomy lessons",
    )
    repository.create(
        "Beta",
        tier=Tier.BORING,
        points=-1,
        tags=["Maya"],
    )
    gamma = repository.create(
        "Gamma",
        tier=Tier.BANGERS,
        points=1,
        tags=["Blender"],
    )

    assert repository.search("anatomy") == [alpha]
    assert repository.search("maya")[0].name == "Beta"
    assert repository.list(tags=["BLENDER"], sort_by="points", descending=True) == [alpha, gamma]
    assert repository.list(tier=Tier.BANGERS) == [gamma]

    updated = repository.update(
        gamma.id,
        name="Gamma Prime",
        tags=["Blender", "Environment"],
        heavy_status=HeavyStatus.YES,
        is_compressed=True,
        last_updated=date(2026, 1, 2),
    )
    assert updated.name == "Gamma Prime"
    assert updated.heavy_status_value is HeavyStatus.YES
    assert updated.tag_names == ["Blender", "Environment"]
    assert updated.last_updated == date(2026, 1, 2)

    with pytest.raises(ValueError, match="Unsupported artist fields"):
        repository.update(gamma.id, points=99)
    with pytest.raises(DuplicateArtistError):
        repository.update(gamma.id, name="alpha")
    with pytest.raises(ValueError):
        repository.update(gamma.id, heavy_status="sometimes")


def test_trash_restore_and_permanent_delete(session: Session) -> None:
    repository = ArtistRepository(session)
    artist = repository.create("Disposable", tags=["Shared"])

    with pytest.raises(ValueError, match="Trash"):
        repository.permanent_delete(artist.id)

    repository.trash(artist.id)
    assert repository.list() == []
    assert repository.list(deleted_only=True) == [artist]
    with pytest.raises(ArtistNotFoundError):
        repository.get(artist.id)
    assert repository.get(artist.id, include_deleted=True) is artist

    repository.restore(artist.id)
    assert repository.list() == [artist]

    repository.trash(artist.id)
    repository.permanent_delete(artist.id)
    with pytest.raises(ArtistNotFoundError):
        repository.get(artist.id, include_deleted=True)


def test_permanent_delete_orders_active_and_reversed_rule_dependencies(
    session: Session,
) -> None:
    repository = ArtistRepository(session)
    active = repository.create("Active effect", heavy_status=HeavyStatus.YES)
    reversed_artist = repository.create("Reversed effect", heavy_status=HeavyStatus.YES)
    service = RatingService(session)

    for artist in (active, reversed_artist):
        service.apply_rule_suggestions(
            service.evaluate_rules(date(2026, 1, 1), [artist.id]),
            date(2026, 1, 1),
        )
    repository.update(reversed_artist.id, is_compressed=True)
    service.apply_rule_suggestions(
        service.evaluate_rules(date(2026, 1, 1), [reversed_artist.id]),
        date(2026, 1, 1),
    )

    effects = session.scalars(select(RuleEffect)).all()
    assert len(effects) == 2
    assert sorted(effect.active for effect in effects) == [False, True]
    assert any(effect.reversal_point_event_id is not None for effect in effects)

    for artist in (active, reversed_artist):
        repository.trash(artist.id)
        repository.permanent_delete(artist.id)

    assert session.scalars(select(Artist)).all() == []
    assert session.scalars(select(PointEvent)).all() == []
    assert session.scalars(select(RuleEffect)).all() == []


def test_tag_filter_requires_every_requested_tag(session: Session) -> None:
    repository = ArtistRepository(session)
    both = repository.create("Both", tags=["A", "B"])
    repository.create("Only A", tags=["A"])

    assert repository.list(tags=["a", "b"]) == [both]


def test_validation_and_safe_paging(session: Session) -> None:
    repository = ArtistRepository(session)
    with pytest.raises(ValueError, match="name cannot be empty"):
        repository.create("   ")
    with pytest.raises(ValueError):
        repository.create("Invalid heavy", heavy_status="sometimes")

    for name in ["Charlie", "Alpha", "Bravo"]:
        repository.create(name)
    assert [artist.name for artist in repository.list(limit=2, offset=1)] == ["Bravo", "Charlie"]

    with pytest.raises(ValueError, match="Unsupported sort"):
        repository.list(sort_by="unknown")
    with pytest.raises(ValueError, match="limit"):
        repository.list(limit=-1)
