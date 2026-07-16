from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, delete, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from collection_manager.constants import CollectionKind, HeavyStatus, SizeQualifier, Tier
from collection_manager.models import (
    Artist,
    ArtistTag,
    PointEvent,
    RuleEffect,
    Tag,
    TierEvent,
    utc_now,
)
from collection_manager.names import display_name, name_key, tag_key


class ArtistNotFoundError(LookupError):
    """Raised when an artist is absent from the requested repository view."""


class DuplicateArtistError(ValueError):
    """Raised when a canonical artist name is already in use."""


class ArtistRepository:
    """Persistence operations for artist records.

    The repository flushes changes so generated identifiers are immediately available, but it
    deliberately never commits. Transaction ownership remains with the caller (normally
    :class:`collection_manager.database.Database`). Point and tier mutations should go through
    ``RatingService`` so their audit ledgers stay complete.
    """

    _EDITABLE_FIELDS = {
        "last_updated",
        "date_evaluated",
        "date_added",
        "size_value",
        "size_unit",
        "size_qualifier",
        "heavy_status",
        "is_compressed",
        "notes",
        "reference_url",
        "folder_path",
    }
    _SORT_COLUMNS = {
        "name": Artist.name,
        "tier": Artist.tier,
        "points": Artist.points,
        "last_updated": Artist.last_updated,
        "date_added": Artist.date_added,
        "created_at": Artist.created_at,
    }

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        name: str,
        *,
        collection_kind: CollectionKind | str = CollectionKind.VIDEOS,
        tier: Tier | str = Tier.WORTH_REVISITING,
        points: int = 0,
        tags: Iterable[str] = (),
        last_updated: date | None = None,
        date_evaluated: date | None = None,
        date_added: date | None = None,
        size_value: Decimal | int | float | str | None = None,
        size_unit: str | None = None,
        size_qualifier: SizeQualifier | str | None = None,
        heavy_status: HeavyStatus | str = HeavyStatus.NO,
        is_compressed: bool = False,
        notes: str = "",
        reference_url: str | None = None,
        folder_path: str | None = None,
    ) -> Artist:
        clean_name = self._validated_name(name)
        key = name_key(clean_name)
        kind = self._collection_kind_value(collection_kind)
        self._ensure_name_available(key, kind)

        artist = Artist(
            collection_kind=kind,
            name=clean_name,
            name_key=key,
            tier=self._tier_value(tier),
            points=int(points),
            last_updated=last_updated,
            date_evaluated=date_evaluated,
            date_added=date_added or date.today(),
            size_value=Decimal(str(size_value)) if size_value is not None else None,
            size_unit=self._optional_text(size_unit),
            size_qualifier=(
                SizeQualifier(size_qualifier).value if size_qualifier is not None else None
            ),
            heavy_status=HeavyStatus(heavy_status).value,
            is_compressed=bool(is_compressed),
            notes=notes or "",
            reference_url=self._optional_text(reference_url),
            folder_path=self._optional_text(folder_path),
        )
        self.session.add(artist)
        self.session.flush()
        self.set_tags(artist.id, tags)
        return artist

    # Friendly aliases used by service and UI callers.
    create_artist = create

    def get(self, artist_id: int, *, include_deleted: bool = False) -> Artist:
        statement = (
            select(Artist)
            .options(selectinload(Artist.tags).selectinload(ArtistTag.tag))
            .where(Artist.id == artist_id)
        )
        if not include_deleted:
            statement = statement.where(Artist.deleted_at.is_(None))
        artist = self.session.scalar(statement)
        if artist is None:
            raise ArtistNotFoundError(f"Artist {artist_id} was not found")
        return artist

    get_artist = get

    def find_by_name(
        self,
        name: str,
        *,
        collection_kind: CollectionKind | str = CollectionKind.VIDEOS,
        include_deleted: bool = False,
    ) -> Artist | None:
        statement = (
            select(Artist)
            .options(selectinload(Artist.tags).selectinload(ArtistTag.tag))
            .where(
                Artist.collection_kind == self._collection_kind_value(collection_kind),
                Artist.name_key == name_key(name),
            )
        )
        if not include_deleted:
            statement = statement.where(Artist.deleted_at.is_(None))
        return self.session.scalar(statement)

    def list(
        self,
        *,
        collection_kind: CollectionKind | str | None = CollectionKind.VIDEOS,
        search: str | None = None,
        tier: Tier | str | None = None,
        tags: Iterable[str] | None = None,
        include_deleted: bool = False,
        deleted_only: bool = False,
        attention_only: bool = False,
        sort_by: str = "name",
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Artist]:
        if sort_by not in self._SORT_COLUMNS:
            choices = ", ".join(sorted(self._SORT_COLUMNS))
            raise ValueError(f"Unsupported sort column {sort_by!r}; expected one of {choices}")
        if limit is not None and limit < 0:
            raise ValueError("limit cannot be negative")
        if offset < 0:
            raise ValueError("offset cannot be negative")

        statement: Select[tuple[Artist]] = select(Artist).options(
            selectinload(Artist.tags).selectinload(ArtistTag.tag)
        )
        if collection_kind is not None:
            statement = statement.where(
                Artist.collection_kind == self._collection_kind_value(collection_kind)
            )
        if deleted_only:
            statement = statement.where(Artist.deleted_at.is_not(None))
        elif not include_deleted:
            statement = statement.where(Artist.deleted_at.is_(None))

        if tier is not None:
            statement = statement.where(Artist.tier == self._tier_value(tier))

        tag_keys = [tag_key(value) for value in tags or () if display_name(value)]
        for wanted_tag in dict.fromkeys(tag_keys):
            statement = statement.where(
                exists(
                    select(ArtistTag.artist_id)
                    .join(Tag, Tag.id == ArtistTag.tag_id)
                    .where(
                        ArtistTag.artist_id == Artist.id,
                        Tag.name_key == wanted_tag,
                    )
                )
            )

        clean_search = display_name(search or "")
        if clean_search:
            pattern = f"%{self._escape_like(clean_search.casefold())}%"
            tag_match = exists(
                select(ArtistTag.artist_id)
                .join(Tag, Tag.id == ArtistTag.tag_id)
                .where(
                    ArtistTag.artist_id == Artist.id,
                    or_(
                        func.lower(Tag.name).like(pattern, escape="\\"),
                        Tag.name_key.like(pattern, escape="\\"),
                    ),
                )
            )
            statement = statement.where(
                or_(
                    func.lower(Artist.name).like(pattern, escape="\\"),
                    Artist.name_key.like(pattern, escape="\\"),
                    func.lower(Artist.notes).like(pattern, escape="\\"),
                    tag_match,
                )
            )

        if attention_only:
            statement = statement.where(
                exists(
                    select(PointEvent.id).where(
                        PointEvent.artist_id == Artist.id,
                        PointEvent.pending_tier_shift.is_(True),
                        PointEvent.reversed_event_id.is_(None),
                    )
                )
            )

        sort_column = self._SORT_COLUMNS[sort_by]
        ordering = sort_column.desc() if descending else sort_column.asc()
        statement = statement.order_by(ordering, Artist.name_key.asc(), Artist.id.asc())
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.session.scalars(statement).unique())

    list_artists = list

    def search(self, query: str, **filters: Any) -> list[Artist]:
        """Search artist names, notes, and tags while accepting the usual list filters."""

        return self.list(search=query, **filters)

    def update(
        self,
        artist_id: int,
        *,
        name: str | None = None,
        tags: Iterable[str] | None = None,
        **changes: Any,
    ) -> Artist:
        unknown = set(changes) - self._EDITABLE_FIELDS
        if unknown:
            raise ValueError("Unsupported artist fields: " + ", ".join(sorted(unknown)))
        artist = self.get(artist_id, include_deleted=True)

        if name is not None:
            clean_name = self._validated_name(name)
            key = name_key(clean_name)
            self._ensure_name_available(
                key,
                artist.collection_kind,
                excluding_artist_id=artist.id,
            )
            artist.name = clean_name
            artist.name_key = key

        for field_name, value in changes.items():
            if field_name == "size_value":
                value = Decimal(str(value)) if value is not None else None
            elif field_name == "size_qualifier":
                value = SizeQualifier(value).value if value is not None else None
            elif field_name == "heavy_status":
                if value is None:
                    raise ValueError("heavy_status cannot be null")
                value = HeavyStatus(value).value
            elif field_name in {"size_unit", "reference_url", "folder_path"}:
                value = self._optional_text(value)
            elif field_name == "notes":
                value = value or ""
            setattr(artist, field_name, value)

        if tags is not None:
            self.set_tags(artist.id, tags)
        self.session.flush()
        return artist

    update_artist = update

    def set_tags(self, artist_id: int, tags: Iterable[str]) -> list[str]:
        artist = self.get(artist_id, include_deleted=True)
        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for value in tags:
            clean = display_name(value)
            if not clean:
                continue
            key = tag_key(clean)
            if key in seen:
                continue
            seen.add(key)
            normalized.append((key, clean))

        # Flush removals before re-adding an existing tag so SQLite's composite unique
        # constraint is never briefly violated.
        artist.tags.clear()
        self.session.flush()
        for position, (key, clean) in enumerate(normalized):
            tag = self.session.scalar(select(Tag).where(Tag.name_key == key))
            if tag is None:
                tag = Tag(name_key=key, name=clean)
                self.session.add(tag)
                self.session.flush()
            artist.tags.append(ArtistTag(tag=tag, position=position))
        self.session.flush()
        return artist.tag_names

    def trash(self, artist_id: int) -> Artist:
        artist = self.get(artist_id)
        artist.deleted_at = utc_now()
        self.session.flush()
        return artist

    def restore(self, artist_id: int) -> Artist:
        artist = self.get(artist_id, include_deleted=True)
        if artist.deleted_at is None:
            return artist
        artist.deleted_at = None
        self.session.flush()
        return artist

    def permanent_delete(self, artist_id: int, *, force: bool = False) -> None:
        artist = self.get(artist_id, include_deleted=True)
        if artist.deleted_at is None and not force:
            raise ValueError("An artist must be moved to Trash before permanent deletion")

        # The event schema intentionally preserves strict foreign keys between rule effects,
        # tier changes, and point events. Delete those ledgers in dependency order before the
        # artist row; relying on sibling ORM cascades can otherwise attempt PointEvent first and
        # violate RuleEffect.point_event_id when SQLite foreign keys are enabled.
        self.session.execute(delete(RuleEffect).where(RuleEffect.artist_id == artist.id))
        self.session.execute(delete(TierEvent).where(TierEvent.artist_id == artist.id))
        self.session.execute(delete(PointEvent).where(PointEvent.artist_id == artist.id))
        self.session.execute(delete(ArtistTag).where(ArtistTag.artist_id == artist.id))
        self.session.execute(delete(Artist).where(Artist.id == artist.id))
        self.session.flush()

    def point_history(self, artist_id: int) -> Sequence[PointEvent]:
        self.get(artist_id, include_deleted=True)
        return list(
            self.session.scalars(
                select(PointEvent)
                .where(PointEvent.artist_id == artist_id)
                .order_by(PointEvent.created_at.asc(), PointEvent.id.asc())
            )
        )

    def tier_history(self, artist_id: int) -> Sequence[TierEvent]:
        self.get(artist_id, include_deleted=True)
        return list(
            self.session.scalars(
                select(TierEvent)
                .where(TierEvent.artist_id == artist_id)
                .order_by(TierEvent.created_at.asc(), TierEvent.id.asc())
            )
        )

    def _ensure_name_available(
        self,
        key: str,
        collection_kind: CollectionKind | str,
        excluding_artist_id: int | None = None,
    ) -> None:
        statement = select(Artist.id).where(
            Artist.collection_kind == self._collection_kind_value(collection_kind),
            Artist.name_key == key,
        )
        if excluding_artist_id is not None:
            statement = statement.where(Artist.id != excluding_artist_id)
        if self.session.scalar(statement) is not None:
            raise DuplicateArtistError("An artist with this canonical name already exists")

    @staticmethod
    def _validated_name(value: str) -> str:
        clean = display_name(value)
        if not clean:
            raise ValueError("Artist name cannot be empty")
        if len(clean) > 300:
            raise ValueError("Artist name cannot exceed 300 characters")
        return clean

    @staticmethod
    def _tier_value(value: Tier | str) -> str:
        return Tier(value).value

    @staticmethod
    def _collection_kind_value(value: CollectionKind | str) -> str:
        return CollectionKind(value).value

    @staticmethod
    def _optional_text(value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip()
        return clean or None

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
