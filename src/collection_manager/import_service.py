from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from collection_manager.constants import (
    CollectionKind,
    HeavyStatus,
    PointEventKind,
    SizeQualifier,
    Tier,
)
from collection_manager.database import Database
from collection_manager.domain import ArtistResolution, DuplicateGroup, ImportPreview, ParsedArtist
from collection_manager.models import (
    Artist,
    ArtistTag,
    ImportResolution,
    ImportRun,
    PointEvent,
    Tag,
    TierEvent,
)
from collection_manager.names import display_name, name_key, tag_key
from collection_manager.parsing import preview_import


class ImportValidationError(ValueError):
    pass


class UnresolvedDuplicatesError(ImportValidationError):
    def __init__(self, groups: Sequence[DuplicateGroup]):
        self.groups = tuple(groups)
        super().__init__(f"{len(self.groups)} duplicate group(s) still require a decision")


class UnparseableLinesError(ImportValidationError):
    def __init__(self, lines: Sequence[tuple[int, str, str]]):
        self.lines = tuple(lines)
        super().__init__(f"{len(self.lines)} unparseable source line(s) must be corrected")


class InvalidResolutionError(ImportValidationError):
    pass


@dataclass(slots=True, frozen=True)
class ImportConflict:
    artist_id: int
    name: str
    name_key: str
    imported: ParsedArtist
    changed_fields: tuple[str, ...]


class ImportConflictsError(ImportValidationError):
    def __init__(self, conflicts: Sequence[ImportConflict]):
        self.conflicts = tuple(conflicts)
        super().__init__(f"{len(self.conflicts)} changed artist(s) require a merge decision")


@dataclass(slots=True, frozen=True)
class ImportResult:
    import_run_id: int
    collection_kind: CollectionKind
    added: int
    updated: int
    skipped: int
    kept: int
    artist_ids: tuple[int, ...]
    backup_path: Path | None


@dataclass(slots=True, frozen=True)
class ResolvedArtist:
    artist: ParsedArtist
    member_indexes: tuple[int, ...]
    source_names: tuple[str, ...]
    decision: str


class ImportService:
    """Preview, validate, and transactionally persist text imports."""

    def __init__(
        self,
        database: Database,
        collection_kind: CollectionKind | str,
        backup_dir: Path | None = None,
    ):
        self.database = database
        self.collection_kind = CollectionKind(collection_kind)
        self.backup_dir = backup_dir

    def preview_import(self, path: Path | str) -> ImportPreview:
        return preview_import(path)

    def find_conflicts(
        self,
        preview: ImportPreview,
        resolutions: Sequence[ArtistResolution] = (),
    ) -> list[ImportConflict]:
        resolved = resolve_preview(preview, resolutions)
        with self.database.session() as session:
            existing = _existing_by_key(session, self.collection_kind)
            return _find_conflicts(resolved, existing)

    def apply_import(
        self,
        preview: ImportPreview,
        resolutions: Sequence[ArtistResolution] = (),
        conflict_resolutions: Mapping[str, str] | None = None,
        *,
        create_backup: bool = True,
        allow_unparseable: bool = False,
    ) -> ImportResult:
        if preview.unparseable and not allow_unparseable:
            raise UnparseableLinesError(preview.unparseable)
        resolved = resolve_preview(preview, resolutions)
        decisions = _normalize_conflict_decisions(conflict_resolutions or {})

        with self.database.session() as session:
            conflicts = _find_conflicts(
                resolved,
                _existing_by_key(session, self.collection_kind),
            )
        missing_decisions = [item for item in conflicts if item.name_key not in decisions]
        if missing_decisions:
            raise ImportConflictsError(missing_decisions)

        backup_path = None
        if create_backup:
            backup_path = self.database.backup(self.backup_dir, reason="pre-import")

        added = 0
        updated = 0
        skipped = 0
        kept = 0
        artist_ids: list[int] = []
        with self.database.session() as session:
            existing = _existing_by_key(session, self.collection_kind)
            tags = {tag.name_key: tag for tag in session.scalars(select(Tag))}
            run = ImportRun(
                collection_kind=self.collection_kind.value,
                source_path=str(preview.source_path),
                source_sha256=preview.sha256,
                source_line_count=len(preview.artists) + len(preview.unparseable),
                artist_count=len(resolved),
                warnings=_all_warnings(preview),
            )
            session.add(run)
            session.flush()

            for candidate in resolved:
                parsed = candidate.artist
                key = name_key(parsed.name)
                model = existing.get(key)
                changed_fields = _changed_fields(model, parsed) if model is not None else ()
                operation: str
                if model is None:
                    model = _create_artist(parsed, self.collection_kind)
                    session.add(model)
                    _set_tags(model, parsed.tags, tags, session)
                    if parsed.points:
                        model.point_events.append(
                            PointEvent(
                                kind=PointEventKind.LEGACY_OPENING.value,
                                delta=parsed.points,
                                reason="Legacy opening balance",
                                old_points=0,
                                new_points=parsed.points,
                                pending_tier_shift=_has_legal_shift(parsed.tier, parsed.points),
                            )
                        )
                    session.flush()
                    existing[key] = model
                    added += 1
                    operation = "added"
                elif not changed_fields:
                    skipped += 1
                    operation = "unchanged"
                else:
                    decision = decisions[key]
                    if decision == "keep":
                        skipped += 1
                        kept += 1
                        operation = "kept_database"
                    else:
                        _apply_merge(session, model, parsed, changed_fields)
                        _set_tags(model, parsed.tags, tags, session)
                        session.flush()
                        updated += 1
                        operation = "applied_import"

                artist_ids.append(model.id)
                source_members = [preview.artists[index] for index in candidate.member_indexes]
                session.add(
                    ImportResolution(
                        import_run_id=run.id,
                        artist_id=model.id,
                        canonical_name=model.name,
                        source_lines=[member.source_line for member in source_members],
                        source_names=list(candidate.source_names),
                        decision=candidate.decision,
                        details={
                            "operation": operation,
                            "changed_fields": list(changed_fields),
                            "warnings": list(parsed.warnings),
                            "raw_lines": [member.raw_line for member in source_members],
                        },
                    )
                )

            session.flush()
            run_id = run.id

        return ImportResult(
            import_run_id=run_id,
            collection_kind=self.collection_kind,
            added=added,
            updated=updated,
            skipped=skipped,
            kept=kept,
            artist_ids=tuple(artist_ids),
            backup_path=backup_path,
        )


def apply_import(
    database: Database,
    preview: ImportPreview,
    resolutions: Sequence[ArtistResolution] = (),
    conflict_resolutions: Mapping[str, str] | None = None,
    *,
    collection_kind: CollectionKind | str,
    **kwargs,
) -> ImportResult:
    """Convenience wrapper for callers that do not retain a service object."""

    return ImportService(database, collection_kind).apply_import(
        preview,
        resolutions,
        conflict_resolutions,
        **kwargs,
    )


def resolve_preview(
    preview: ImportPreview,
    resolutions: Sequence[ArtistResolution] = (),
) -> list[ResolvedArtist]:
    """Apply explicit duplicate decisions without performing persistence."""

    group_by_members = {
        frozenset(group.member_indexes): group for group in preview.duplicate_groups
    }
    resolution_by_members: dict[frozenset[int], ArtistResolution] = {}
    for resolution in resolutions:
        key = frozenset(resolution.member_indexes)
        if key not in group_by_members:
            raise InvalidResolutionError("A resolution does not match a preview duplicate group")
        if key in resolution_by_members:
            raise InvalidResolutionError("A duplicate group has more than one resolution")
        resolution_by_members[key] = resolution

    unresolved = [
        group for members, group in group_by_members.items() if members not in resolution_by_members
    ]
    if unresolved:
        raise UnresolvedDuplicatesError(unresolved)

    grouped_indexes = {
        index for group in preview.duplicate_groups for index in group.member_indexes
    }
    candidates: list[ResolvedArtist] = []
    for index, artist in enumerate(preview.artists):
        if index not in grouped_indexes:
            candidates.append(
                ResolvedArtist(
                    artist=artist,
                    member_indexes=(index,),
                    source_names=(artist.name,),
                    decision="unique",
                )
            )

    for group in preview.duplicate_groups:
        resolution = resolution_by_members[frozenset(group.member_indexes)]
        source_names = tuple(preview.artists[index].name for index in group.member_indexes)
        if resolution.different_artists:
            for index in group.member_indexes:
                candidates.append(
                    ResolvedArtist(
                        artist=preview.artists[index],
                        member_indexes=(index,),
                        source_names=(preview.artists[index].name,),
                        decision="different",
                    )
                )
        else:
            if frozenset(resolution.member_indexes) != frozenset(group.member_indexes):
                raise InvalidResolutionError("Resolution members do not match the duplicate group")
            if not display_name(resolution.canonical.name):
                raise InvalidResolutionError("A canonical artist name cannot be empty")
            candidates.append(
                ResolvedArtist(
                    artist=resolution.canonical,
                    member_indexes=group.member_indexes,
                    source_names=source_names,
                    decision="merged",
                )
            )

    candidates.sort(key=lambda item: min(item.member_indexes))
    seen_names: dict[str, str] = {}
    for candidate in candidates:
        key = name_key(candidate.artist.name)
        if key in seen_names:
            raise InvalidResolutionError(
                f"Resolved artist names are not unique: {seen_names[key]!r} and "
                f"{candidate.artist.name!r}"
            )
        seen_names[key] = candidate.artist.name
    return candidates


def _existing_by_key(
    session: Session,
    collection_kind: CollectionKind | str,
) -> dict[str, Artist]:
    statement = (
        select(Artist)
        .options(selectinload(Artist.tags).selectinload(ArtistTag.tag))
        .where(Artist.collection_kind == CollectionKind(collection_kind).value)
    )
    return {artist.name_key: artist for artist in session.scalars(statement).unique()}


def _find_conflicts(
    candidates: Sequence[ResolvedArtist], existing: Mapping[str, Artist]
) -> list[ImportConflict]:
    conflicts: list[ImportConflict] = []
    for candidate in candidates:
        parsed = candidate.artist
        key = name_key(parsed.name)
        model = existing.get(key)
        if model is None:
            continue
        fields = _changed_fields(model, parsed)
        if fields:
            conflicts.append(
                ImportConflict(
                    artist_id=model.id,
                    name=model.name,
                    name_key=key,
                    imported=parsed,
                    changed_fields=fields,
                )
            )
    return conflicts


def _changed_fields(model: Artist, parsed: ParsedArtist) -> tuple[str, ...]:
    fields: list[str] = []
    comparisons = (
        ("name", model.name, display_name(parsed.name)),
        ("tier", model.tier, _tier_value(parsed.tier)),
        ("points", model.points, parsed.points),
        ("last_updated", model.last_updated, parsed.last_updated),
        ("size_value", model.size_value, parsed.size_value),
        ("size_unit", model.size_unit, parsed.size_unit),
        (
            "size_qualifier",
            model.size_qualifier,
            _size_qualifier_value(parsed.size_qualifier),
        ),
        ("heavy_status", model.heavy_status, _heavy_status_value(parsed.heavy_status)),
        ("is_compressed", model.is_compressed, parsed.is_compressed),
        ("notes", model.notes or "", parsed.notes),
        ("reference_url", model.reference_url, parsed.reference_url),
    )
    for field, current, imported in comparisons:
        if field == "size_value" and current is not None and imported is not None:
            different = current != imported
        else:
            different = current != imported
        if different:
            fields.append(field)
    current_tags = tuple(tag_key(value) for value in model.tag_names)
    imported_tags = tuple(tag_key(value) for value in parsed.tags)
    if current_tags != imported_tags:
        fields.append("tags")
    return tuple(fields)


def _create_artist(
    parsed: ParsedArtist,
    collection_kind: CollectionKind | str,
) -> Artist:
    return Artist(
        collection_kind=CollectionKind(collection_kind).value,
        name_key=name_key(parsed.name),
        name=display_name(parsed.name),
        tier=_tier_value(parsed.tier),
        points=parsed.points,
        last_updated=parsed.last_updated,
        size_value=parsed.size_value,
        size_unit=parsed.size_unit,
        size_qualifier=_size_qualifier_value(parsed.size_qualifier),
        heavy_status=_heavy_status_value(parsed.heavy_status),
        is_compressed=parsed.is_compressed,
        notes=parsed.notes,
        reference_url=parsed.reference_url,
    )


def _apply_merge(
    session: Session,
    model: Artist,
    parsed: ParsedArtist,
    changed_fields: Sequence[str],
) -> None:
    old_points = model.points
    old_tier = model.tier
    if "points" in changed_fields:
        session.add(
            PointEvent(
                artist_id=model.id,
                kind=PointEventKind.LEGACY_MERGE.value,
                delta=parsed.points - old_points,
                reason="Legacy merge selected imported point balance",
                old_points=old_points,
                new_points=parsed.points,
                pending_tier_shift=_has_legal_shift(parsed.tier, parsed.points),
            )
        )
    if "tier" in changed_fields:
        session.add(
            TierEvent(
                artist_id=model.id,
                old_tier=old_tier,
                new_tier=_tier_value(parsed.tier),
                reason="Legacy merge selected imported tier",
                points_before_reset=old_points,
            )
        )

    model.name_key = name_key(parsed.name)
    model.name = display_name(parsed.name)
    model.tier = _tier_value(parsed.tier)
    model.points = parsed.points
    model.last_updated = parsed.last_updated
    model.size_value = parsed.size_value
    model.size_unit = parsed.size_unit
    model.size_qualifier = _size_qualifier_value(parsed.size_qualifier)
    model.heavy_status = _heavy_status_value(parsed.heavy_status)
    model.is_compressed = parsed.is_compressed
    model.notes = parsed.notes
    model.reference_url = parsed.reference_url


def _set_tags(
    artist: Artist,
    names: Sequence[str],
    tags: dict[str, Tag],
    session: Session,
) -> None:
    existing_links = {link.tag.name_key: link for link in artist.tags}
    links: list[ArtistTag] = []
    seen: set[str] = set()
    for value in names:
        cleaned = " ".join(value.strip().split())
        key = tag_key(cleaned)
        if not cleaned or key in seen:
            continue
        seen.add(key)
        tag = tags.get(key)
        if tag is None:
            tag = Tag(name_key=key, name=cleaned)
            session.add(tag)
            tags[key] = tag
        link = existing_links.get(key)
        if link is None:
            link = ArtistTag(tag=tag)
        link.position = len(links)
        links.append(link)
    artist.tags = links


def _normalize_conflict_decisions(values: Mapping[str, str]) -> dict[str, str]:
    decisions: dict[str, str] = {}
    for raw_key, raw_decision in values.items():
        key = name_key(raw_key)
        decision = raw_decision.strip().casefold().replace("-", "_")
        if decision in {"keep", "keep_database", "database"}:
            decisions[key] = "keep"
        elif decision in {"import", "apply_import", "imported"}:
            decisions[key] = "import"
        else:
            raise ImportValidationError(f"Unknown conflict decision: {raw_decision!r}")
    return decisions


def _has_legal_shift(tier: Tier | str, points: int) -> bool:
    tier = Tier(tier)
    return (points >= 3 and tier is not Tier.CREAM) or (points <= -3 and tier is not Tier.BORING)


def _tier_value(value: Tier | str) -> str:
    return Tier(value).value


def _size_qualifier_value(value: SizeQualifier | str | None) -> str | None:
    return SizeQualifier(value).value if value is not None else None


def _heavy_status_value(value: HeavyStatus | str) -> str:
    return HeavyStatus(value).value


def _all_warnings(preview: ImportPreview) -> list[str]:
    warnings = list(preview.global_warnings)
    for artist in preview.artists:
        warnings.extend(f"Line {artist.source_line}: {warning}" for warning in artist.warnings)
    warnings.extend(
        f"Line {line_number}: {reason}" for line_number, _raw_line, reason in preview.unparseable
    )
    return list(dict.fromkeys(warnings))
