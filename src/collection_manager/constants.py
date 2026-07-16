from __future__ import annotations

from enum import StrEnum


class CollectionKind(StrEnum):
    VIDEOS = "videos"
    IMAGES = "images"


class Tier(StrEnum):
    BORING = "Boring"
    FELL_OFF = "Fell Off"
    WORTH_REVISITING = "Worth Revisiting"
    UNSAVABLE_BANGERS = "Unsavable Bangers"
    BANGERS = "Bangers"
    CREAM = "Cream of the Crop"


TIER_ASCENDING: tuple[Tier, ...] = (
    Tier.BORING,
    Tier.FELL_OFF,
    Tier.WORTH_REVISITING,
    Tier.UNSAVABLE_BANGERS,
    Tier.BANGERS,
    Tier.CREAM,
)
TIER_DESCENDING: tuple[Tier, ...] = tuple(reversed(TIER_ASCENDING))

TIER_COLORS: dict[Tier, str] = {
    Tier.BORING: "#6b7280",
    Tier.FELL_OFF: "#9f6262",
    Tier.WORTH_REVISITING: "#b98b3d",
    Tier.UNSAVABLE_BANGERS: "#a76fd4",
    Tier.BANGERS: "#4f8fd8",
    Tier.CREAM: "#d5ad42",
}


class HeavyStatus(StrEnum):
    NO = "no"
    YES = "yes"
    UNKNOWN = "unknown"


class SizeQualifier(StrEnum):
    EXACT = "exact"
    AT_LEAST = "at_least"
    APPROXIMATE = "approximate"


class PointEventKind(StrEnum):
    LEGACY_OPENING = "legacy_opening"
    LEGACY_MERGE = "legacy_merge"
    UPDATE_VIBE = "update_vibe"
    RULE_ADJUSTMENT = "rule_adjustment"
    MANUAL_ADJUSTMENT = "manual_adjustment"
    TIER_RESET = "tier_reset"
    REVERSAL = "reversal"


class RuleKind(StrEnum):
    INACTIVITY = "inactivity"
    HEAVY = "heavy"
    DIVERSITY = "diversity"
