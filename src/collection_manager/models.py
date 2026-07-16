from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from collection_manager.constants import CollectionKind, HeavyStatus, SizeQualifier, Tier


def utc_now() -> datetime:
    # SQLite stores these audit timestamps as UTC-naive values for sortable portability.
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Artist(Base):
    __tablename__ = "artists"
    __table_args__ = (
        Index(
            "ux_artists_collection_kind_name_key",
            "collection_kind",
            "name_key",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collection_kind: Mapped[str] = mapped_column(
        String(20),
        default=CollectionKind.VIDEOS.value,
        server_default=CollectionKind.VIDEOS.value,
        index=True,
    )
    name_key: Mapped[str] = mapped_column(String(300))
    name: Mapped[str] = mapped_column(String(300), index=True)
    tier: Mapped[str] = mapped_column(String(40), index=True, default=Tier.WORTH_REVISITING.value)
    points: Mapped[int] = mapped_column(Integer, default=0)
    last_updated: Mapped[date | None] = mapped_column(Date)
    date_evaluated: Mapped[date | None] = mapped_column(Date)
    date_added: Mapped[date] = mapped_column(Date, default=date.today)
    size_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    size_unit: Mapped[str | None] = mapped_column(String(8))
    size_qualifier: Mapped[str | None] = mapped_column(String(20))
    heavy_status: Mapped[str] = mapped_column(String(12), default=HeavyStatus.NO.value)
    is_compressed: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    reference_url: Mapped[str | None] = mapped_column(Text)
    folder_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)

    tags: Mapped[list[ArtistTag]] = relationship(
        back_populates="artist", cascade="all, delete-orphan", order_by="ArtistTag.position"
    )
    point_events: Mapped[list[PointEvent]] = relationship(
        back_populates="artist", cascade="all, delete-orphan", order_by="PointEvent.created_at"
    )
    tier_events: Mapped[list[TierEvent]] = relationship(
        back_populates="artist", cascade="all, delete-orphan", order_by="TierEvent.created_at"
    )
    rule_effects: Mapped[list[RuleEffect]] = relationship(
        back_populates="artist", cascade="all, delete-orphan"
    )

    @property
    def tier_value(self) -> Tier:
        return Tier(self.tier)

    @property
    def collection_kind_value(self) -> CollectionKind:
        return CollectionKind(self.collection_kind)

    @property
    def heavy_status_value(self) -> HeavyStatus:
        return HeavyStatus(self.heavy_status)

    @property
    def size_qualifier_value(self) -> SizeQualifier | None:
        return SizeQualifier(self.size_qualifier) if self.size_qualifier else None

    @property
    def tag_names(self) -> list[str]:
        return [link.tag.name for link in self.tags]


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))

    artists: Mapped[list[ArtistTag]] = relationship(back_populates="tag")


class ArtistTag(Base):
    __tablename__ = "artist_tags"
    __table_args__ = (UniqueConstraint("artist_id", "tag_id", name="uq_artist_tag"),)

    artist_id: Mapped[int] = mapped_column(
        ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    artist: Mapped[Artist] = relationship(back_populates="tags")
    tag: Mapped[Tag] = relationship(back_populates="artists")


class PointEvent(Base):
    __tablename__ = "point_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    old_points: Mapped[int] = mapped_column(Integer)
    new_points: Mapped[int] = mapped_column(Integer)
    rule_key: Mapped[str | None] = mapped_column(String(120), index=True)
    pending_tier_shift: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    reversed_event_id: Mapped[int | None] = mapped_column(ForeignKey("point_events.id"))

    artist: Mapped[Artist] = relationship(back_populates="point_events")


class TierEvent(Base):
    __tablename__ = "tier_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), index=True)
    old_tier: Mapped[str] = mapped_column(String(40))
    new_tier: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)
    points_before_reset: Mapped[int] = mapped_column(Integer, default=0)
    triggering_point_event_id: Mapped[int | None] = mapped_column(ForeignKey("point_events.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    reversed_event_id: Mapped[int | None] = mapped_column(ForeignKey("tier_events.id"))

    artist: Mapped[Artist] = relationship(back_populates="tier_events")


class RuleEffect(Base):
    __tablename__ = "rule_effects"
    __table_args__ = (UniqueConstraint("artist_id", "rule_key", name="uq_artist_rule_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), index=True)
    rule_kind: Mapped[str] = mapped_column(String(30), index=True)
    rule_key: Mapped[str] = mapped_column(String(120))
    applied_delta: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    point_event_id: Mapped[int] = mapped_column(ForeignKey("point_events.id"))
    reversal_point_event_id: Mapped[int | None] = mapped_column(ForeignKey("point_events.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime)

    artist: Mapped[Artist] = relationship(back_populates="rule_effects")


class ImportRun(Base):
    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collection_kind: Mapped[str] = mapped_column(
        String(20),
        default=CollectionKind.VIDEOS.value,
        server_default=CollectionKind.VIDEOS.value,
        index=True,
    )
    source_path: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str] = mapped_column(String(64), index=True)
    source_line_count: Mapped[int] = mapped_column(Integer)
    artist_count: Mapped[int] = mapped_column(Integer)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)

    resolutions: Mapped[list[ImportResolution]] = relationship(
        back_populates="import_run", cascade="all, delete-orphan"
    )


class ImportResolution(Base):
    __tablename__ = "import_resolutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_run_id: Mapped[int] = mapped_column(
        ForeignKey("import_runs.id", ondelete="CASCADE"), index=True
    )
    artist_id: Mapped[int | None] = mapped_column(ForeignKey("artists.id", ondelete="SET NULL"))
    canonical_name: Mapped[str] = mapped_column(String(300))
    source_lines: Mapped[list[int]] = mapped_column(JSON, default=list)
    source_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    decision: Mapped[str] = mapped_column(String(30), default="unique")
    details: Mapped[dict] = mapped_column(JSON, default=dict)

    import_run: Mapped[ImportRun] = relationship(back_populates="resolutions")
