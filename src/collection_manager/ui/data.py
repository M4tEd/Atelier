from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from collection_manager.constants import CollectionKind, HeavyStatus, SizeQualifier, Tier
from collection_manager.database import Database
from collection_manager.models import Artist, ArtistTag
from collection_manager.repository import ArtistRepository


@dataclass(slots=True, frozen=True)
class HistoryItem:
    created_at: datetime
    category: str
    summary: str
    reason: str


@dataclass(slots=True, frozen=True)
class ArtistView:
    id: int
    collection_kind: CollectionKind
    name: str
    tier: Tier
    points: int
    last_updated: date | None
    date_evaluated: date | None
    date_added: date
    size_value: Decimal | None
    size_unit: str | None
    size_qualifier: SizeQualifier | None
    heavy_status: HeavyStatus
    is_compressed: bool
    tags: tuple[str, ...]
    notes: str
    reference_url: str | None
    folder_path: str | None
    deleted_at: datetime | None
    needs_attention: bool
    history: tuple[HistoryItem, ...]

    @property
    def size_text(self) -> str:
        if self.size_value is None:
            return ""
        prefix = "~" if self.size_qualifier is SizeQualifier.APPROXIMATE else ""
        suffix = "+" if self.size_qualifier is SizeQualifier.AT_LEAST else ""
        numeric = format(self.size_value.normalize(), "f")
        return f"{prefix}{numeric} {self.size_unit or 'GB'}{suffix}"


def _tier(value: str | Tier) -> Tier:
    try:
        return value if isinstance(value, Tier) else Tier(value)
    except ValueError:
        return Tier.WORTH_REVISITING


def _heavy(value: str | HeavyStatus) -> HeavyStatus:
    try:
        return value if isinstance(value, HeavyStatus) else HeavyStatus(value)
    except ValueError:
        return HeavyStatus.UNKNOWN


def _qualifier(value: str | None) -> SizeQualifier | None:
    if not value:
        return None
    try:
        return SizeQualifier(value)
    except ValueError:
        return None


def _collection_kind(value: str | CollectionKind) -> CollectionKind:
    try:
        return value if isinstance(value, CollectionKind) else CollectionKind(value)
    except ValueError:
        return CollectionKind.VIDEOS


class ArtistStore:
    """Small UI-facing transaction boundary returning immutable view data."""

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _query():  # noqa: ANN205 - SQLAlchemy's select type is deliberately internal
        return select(Artist).options(
            selectinload(Artist.tags).selectinload(ArtistTag.tag),
            selectinload(Artist.point_events),
            selectinload(Artist.tier_events),
        )

    def list_artists(
        self,
        collection_kind: CollectionKind = CollectionKind.VIDEOS,
        include_deleted: bool = False,
    ) -> list[ArtistView]:
        with self.database.session() as session:
            query = self._query().where(Artist.collection_kind == collection_kind.value)
            if include_deleted:
                query = query.where(Artist.deleted_at.is_not(None))
            else:
                query = query.where(Artist.deleted_at.is_(None))
            artists = session.scalars(query.order_by(Artist.name_key)).unique().all()
            return [self._view(artist) for artist in artists]

    def get(self, artist_id: int) -> ArtistView | None:
        with self.database.session() as session:
            artist = (
                session.scalars(self._query().where(Artist.id == artist_id)).unique().one_or_none()
            )
            return self._view(artist) if artist else None

    @staticmethod
    def _view(artist: Artist) -> ArtistView:
        point_history = [
            HistoryItem(
                event.created_at,
                "Points",
                f"{event.old_points:+d} → {event.new_points:+d} ({event.delta:+d})",
                event.reason,
            )
            for event in artist.point_events
        ]
        tier_history = [
            HistoryItem(
                event.created_at,
                "Tier",
                f"{event.old_tier} → {event.new_tier}",
                event.reason,
            )
            for event in artist.tier_events
        ]
        history = tuple(
            sorted((*point_history, *tier_history), key=lambda item: item.created_at, reverse=True)
        )
        return ArtistView(
            id=artist.id,
            collection_kind=_collection_kind(artist.collection_kind),
            name=artist.name,
            tier=_tier(artist.tier),
            points=artist.points,
            last_updated=artist.last_updated,
            date_evaluated=artist.date_evaluated,
            date_added=artist.date_added,
            size_value=artist.size_value,
            size_unit=artist.size_unit,
            size_qualifier=_qualifier(artist.size_qualifier),
            heavy_status=_heavy(artist.heavy_status),
            is_compressed=artist.is_compressed,
            tags=tuple(artist.tag_names),
            notes=artist.notes or "",
            reference_url=artist.reference_url,
            folder_path=artist.folder_path,
            deleted_at=artist.deleted_at,
            needs_attention=any(
                event.pending_tier_shift and event.reversed_event_id is None
                for event in artist.point_events
            ),
            history=history,
        )

    def create(
        self,
        values: dict[str, Any],
        collection_kind: CollectionKind = CollectionKind.VIDEOS,
    ) -> int:
        with self.database.session() as session:
            artist = ArtistRepository(session).create(
                str(values.get("name", "")),
                collection_kind=collection_kind,
                tier=values.get("tier", Tier.WORTH_REVISITING),
                points=0,
                tags=values.get("tags", ()),
                last_updated=values.get("last_updated"),
                date_evaluated=values.get("date_evaluated"),
                date_added=values.get("date_added") or date.today(),
                size_value=values.get("size_value"),
                size_unit=values.get("size_unit"),
                size_qualifier=values.get("size_qualifier"),
                heavy_status=values.get("heavy_status", HeavyStatus.NO),
                is_compressed=bool(values.get("is_compressed", False)),
                notes=str(values.get("notes", "")).strip(),
                reference_url=str(values.get("reference_url") or "") or None,
                folder_path=str(values.get("folder_path") or "") or None,
            )
            return artist.id

    def update(self, artist_id: int, values: dict[str, Any]) -> None:
        with self.database.session() as session:
            repository = ArtistRepository(session)
            artist = repository.get(artist_id, include_deleted=True)
            repository.update(
                artist_id,
                name=str(values.get("name", artist.name)),
                tags=values.get("tags", ()),
                last_updated=values.get("last_updated"),
                date_evaluated=values.get("date_evaluated"),
                date_added=values.get("date_added") or artist.date_added,
                size_value=values.get("size_value"),
                size_unit=values.get("size_unit"),
                size_qualifier=values.get("size_qualifier"),
                heavy_status=values.get("heavy_status", HeavyStatus.NO),
                is_compressed=bool(values.get("is_compressed", False)),
                notes=str(values.get("notes", "")).strip(),
                reference_url=str(values.get("reference_url") or "") or None,
                folder_path=str(values.get("folder_path") or "") or None,
            )

    def move_artist(self, artist_id: int, target_collection: CollectionKind) -> None:
        with self.database.session() as session:
            ArtistRepository(session).move_to_collection(artist_id, target_collection)

    def trash(self, artist_id: int) -> None:
        with self.database.session() as session:
            ArtistRepository(session).trash(artist_id)

    def restore(self, artist_id: int) -> None:
        with self.database.session() as session:
            ArtistRepository(session).restore(artist_id)

    def permanent_delete(self, artist_id: int) -> None:
        with self.database.session() as session:
            ArtistRepository(session).permanent_delete(artist_id)
