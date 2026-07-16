from __future__ import annotations

import json
import re
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from collection_manager.constants import (
    TIER_DESCENDING,
    CollectionKind,
    HeavyStatus,
    SizeQualifier,
    Tier,
)
from collection_manager.database import Database
from collection_manager.domain import ParsedArtist
from collection_manager.models import Artist, ArtistTag
from collection_manager.names import name_key
from collection_manager.parsing import infer_heavy_status


class ExportService:
    def __init__(self, database: Database, collection_kind: CollectionKind | str):
        self.database = database
        self.collection_kind = CollectionKind(collection_kind)

    def export_text(self, path: Path | str) -> Path:
        return export_text(self.database, path, collection_kind=self.collection_kind)

    def render(self) -> str:
        with self.database.session() as session:
            return render_export(_load_artists(session, self.collection_kind))


def export_text(
    database_or_session: Database | Session,
    path: Path | str,
    *,
    collection_kind: CollectionKind | str,
) -> Path:
    """Write a canonical UTF-8 export with Windows CRLF line endings."""

    destination = Path(path).expanduser().resolve()
    kind = CollectionKind(collection_kind)
    if isinstance(database_or_session, Database):
        with database_or_session.session() as session:
            document = render_export(_load_artists(session, kind))
    elif isinstance(database_or_session, Session):
        document = render_export(_load_artists(database_or_session, kind))
    else:
        raise TypeError("export_text expects a Database or SQLAlchemy Session")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(document.encode("utf-8"))
    return destination


def render_export(artists: Iterable[Artist | ParsedArtist]) -> str:
    """Render all six tier sections in deterministic canonical form."""

    grouped: dict[Tier, list[Artist | ParsedArtist]] = {tier: [] for tier in TIER_DESCENDING}
    for artist in artists:
        grouped[_tier_of(artist)].append(artist)

    lines: list[str] = []
    for tier_index, tier in enumerate(TIER_DESCENDING):
        if tier_index:
            lines.append("")
        lines.append(tier.value)
        tier_artists = sorted(grouped[tier], key=lambda item: name_key(str(item.name)))
        lines.extend(format_artist_line(artist) for artist in tier_artists)
    return "\r\n".join(lines) + "\r\n"


def format_artist_line(artist: Artist | ParsedArtist) -> str:
    """Format one model/domain artist using the unambiguous labeled syntax."""

    parts = [_format_name(str(artist.name))]
    last_updated = getattr(artist, "last_updated", None)
    if last_updated is not None:
        parts.append(f"[{last_updated.isoformat()}]")

    points = int(getattr(artist, "points", 0))
    if points:
        parts.append(f"(points: {points})")

    size_value = getattr(artist, "size_value", None)
    size_unit = getattr(artist, "size_unit", None)
    size_qualifier = _size_qualifier_of(artist)
    inferred_heavy = HeavyStatus.NO
    if size_value is not None and size_unit and size_qualifier is not None:
        size_text = _format_size(Decimal(size_value), str(size_unit), size_qualifier)
        parts.append(f"(size: {size_text})")
        inferred_heavy = infer_heavy_status(Decimal(size_value), str(size_unit), size_qualifier)

    actual_heavy = _heavy_status_of(artist)
    if actual_heavy is not inferred_heavy:
        parts.append(f"(heavy: {actual_heavy.value})")
    if bool(getattr(artist, "is_compressed", False)):
        parts.append("(compressed: true)")

    notes = str(getattr(artist, "notes", "") or "")
    if notes:
        parts.append(f"(note: {json.dumps(notes, ensure_ascii=False)})")
    reference_url = getattr(artist, "reference_url", None)
    if reference_url:
        parts.append(f"(url: {json.dumps(str(reference_url), ensure_ascii=False)})")

    tags = _tags_of(artist)
    if tags:
        parts.append("[" + ", ".join(_format_tag(tag) for tag in tags) + "]")
    return " ".join(parts)


def _load_artists(
    session: Session,
    collection_kind: CollectionKind | str,
) -> list[Artist]:
    statement = (
        select(Artist)
        .options(selectinload(Artist.tags).selectinload(ArtistTag.tag))
        .where(
            Artist.collection_kind == CollectionKind(collection_kind).value,
            Artist.deleted_at.is_(None),
        )
    )
    return list(session.scalars(statement).unique())


def _tier_of(artist: Artist | ParsedArtist) -> Tier:
    value: Any = artist.tier
    return value if isinstance(value, Tier) else Tier(value)


def _size_qualifier_of(artist: Artist | ParsedArtist) -> SizeQualifier | None:
    value: Any = getattr(artist, "size_qualifier", None)
    if value is None:
        return None
    return value if isinstance(value, SizeQualifier) else SizeQualifier(value)


def _heavy_status_of(artist: Artist | ParsedArtist) -> HeavyStatus:
    value: Any = getattr(artist, "heavy_status", HeavyStatus.NO)
    return value if isinstance(value, HeavyStatus) else HeavyStatus(value)


def _tags_of(artist: Artist | ParsedArtist) -> list[str]:
    if isinstance(artist, ParsedArtist):
        return list(artist.tags)
    return list(artist.tag_names)


def _format_size(value: Decimal, unit: str, qualifier: SizeQualifier) -> str:
    number = format(value, "f")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    prefix = "~" if qualifier is SizeQualifier.APPROXIMATE else ""
    suffix = "+" if qualifier is SizeQualifier.AT_LEAST else ""
    return f"{prefix}{number} {unit.upper()}{suffix}"


def _format_tag(value: str) -> str:
    if (
        value != value.strip()
        or any(character in value for character in ',[]"\\\r\n')
        or not value
        or _looks_like_bracket_metadata(value)
    ):
        return json.dumps(value, ensure_ascii=False)
    return value


def _format_name(value: str) -> str:
    if any(character in value for character in '[]()"\\\r\n'):
        return json.dumps(value, ensure_ascii=False)
    return value


def _looks_like_bracket_metadata(value: str) -> bool:
    stripped = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stripped):
        return True
    if re.fullmatch(
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}",
        stripped,
        re.IGNORECASE,
    ):
        return True
    if re.match(r"^(?:https?|file)://", stripped, re.IGNORECASE):
        return True
    return bool(re.match(r"^(?:vol(?:ume)?\.?|parts?)\s*\d", stripped, re.IGNORECASE))
