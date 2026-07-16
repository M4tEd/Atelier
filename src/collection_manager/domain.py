from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from collection_manager.constants import HeavyStatus, RuleKind, SizeQualifier, Tier


@dataclass(slots=True)
class ParsedArtist:
    source_line: int
    raw_line: str
    tier: Tier
    name: str
    points: int = 0
    last_updated: date | None = None
    size_value: Decimal | None = None
    size_unit: str | None = None
    size_qualifier: SizeQualifier | None = None
    heavy_status: HeavyStatus = HeavyStatus.NO
    is_compressed: bool = False
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    reference_url: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class DuplicateGroup:
    key: str
    member_indexes: tuple[int, ...]
    definite: bool
    reasons: tuple[str, ...]


@dataclass(slots=True)
class ImportPreview:
    source_path: Path
    sha256: str
    artists: list[ParsedArtist]
    duplicate_groups: list[DuplicateGroup]
    global_warnings: list[str] = field(default_factory=list)
    unparseable: list[tuple[int, str, str]] = field(default_factory=list)


@dataclass(slots=True)
class ArtistResolution:
    member_indexes: tuple[int, ...]
    canonical: ParsedArtist
    different_artists: bool = False


@dataclass(slots=True, frozen=True)
class RuleSuggestion:
    artist_id: int
    artist_name: str
    rule_kind: RuleKind
    rule_key: str
    delta: int
    reason: str
    is_reversal: bool = False


@dataclass(slots=True, frozen=True)
class TierShiftProposal:
    artist_id: int
    artist_name: str
    old_tier: Tier
    new_tier: Tier
    points: int
