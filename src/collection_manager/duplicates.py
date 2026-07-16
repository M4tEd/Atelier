from __future__ import annotations

import re
from dataclasses import replace

from collection_manager.domain import ArtistResolution, DuplicateGroup, ParsedArtist
from collection_manager.names import collapsed_name, name_key, tag_key

_VARIANT_SUFFIXES: tuple[tuple[str, ...], ...] = (
    ("early", "tutorials"),
    ("early", "work"),
    ("collection",),
    ("early",),
    ("advanced",),
    ("tutorials",),
    ("free",),
    ("retired",),
    ("archive",),
    ("fundamentals",),
    ("sculpting",),
    ("picks",),
)
_ORGANIZATION_SUFFIXES = {"company", "studio"}


def detect_duplicate_groups(artists: list[ParsedArtist]) -> list[DuplicateGroup]:
    """Return connected groups of exact or conservative likely duplicate names.

    Likely matches are only suggestions. Nothing in this module merges or drops a
    record; callers must collect an explicit :class:`ArtistResolution` for every
    returned group.
    """

    adjacency: dict[int, set[int]] = {index: set() for index in range(len(artists))}
    pair_reasons: dict[tuple[int, int], tuple[str, ...]] = {}
    for left in range(len(artists)):
        for right in range(left + 1, len(artists)):
            reasons = _comparison_reasons(artists[left].name, artists[right].name)
            if not reasons:
                continue
            adjacency[left].add(right)
            adjacency[right].add(left)
            pair_reasons[(left, right)] = reasons

    groups: list[DuplicateGroup] = []
    visited: set[int] = set()
    for start in range(len(artists)):
        if start in visited or not adjacency[start]:
            continue
        stack = [start]
        members: set[int] = set()
        while stack:
            index = stack.pop()
            if index in members:
                continue
            members.add(index)
            visited.add(index)
            stack.extend(adjacency[index] - members)
        ordered_members = tuple(sorted(members))
        reasons = sorted(
            {
                reason
                for (left, right), pair_values in pair_reasons.items()
                if left in members and right in members
                for reason in pair_values
            }
        )
        exact_keys = {name_key(artists[index].name) for index in members}
        signatures = [_variant_signature(artists[index].name) for index in members]
        key = min(signatures) if signatures else name_key(artists[start].name)
        groups.append(
            DuplicateGroup(
                key=key,
                member_indexes=ordered_members,
                definite=len(exact_keys) == 1,
                reasons=tuple(reasons),
            )
        )
    return groups


def suggest_resolution(group: DuplicateGroup, artists: list[ParsedArtist]) -> ArtistResolution:
    """Build an editable, conservative starting point for a resolution screen."""

    if any(index < 0 or index >= len(artists) for index in group.member_indexes):
        raise IndexError("Duplicate group refers to an artist outside the preview")
    members = [artists[index] for index in group.member_indexes]
    if not members:
        raise ValueError("Duplicate group is empty")

    preferred = min(members, key=_preferred_name_score)
    latest_date = max((item.last_updated for item in members if item.last_updated), default=None)
    metadata_source = next(
        (item for item in members if item is preferred and item.size_value is not None),
        None,
    ) or next((item for item in members if item.size_value is not None), preferred)
    url_source = next(
        (item for item in members if item is preferred and item.reference_url),
        None,
    ) or next((item for item in members if item.reference_url), preferred)

    tags: list[str] = []
    seen_tags: set[str] = set()
    notes: list[str] = []
    seen_notes: set[str] = set()
    warnings: list[str] = []
    for member in members:
        for tag in member.tags:
            key = tag_key(tag)
            if key not in seen_tags:
                seen_tags.add(key)
                tags.append(tag)
        if member.notes:
            normalized_note = " ".join(member.notes.split()).casefold()
            if normalized_note not in seen_notes:
                seen_notes.add(normalized_note)
                notes.append(member.notes)
        warnings.extend(member.warnings)
    warnings.append("Suggested duplicate merge requires confirmation")

    canonical = replace(
        preferred,
        name=_canonical_display_name(preferred.name),
        last_updated=latest_date,
        size_value=metadata_source.size_value,
        size_unit=metadata_source.size_unit,
        size_qualifier=metadata_source.size_qualifier,
        heavy_status=metadata_source.heavy_status,
        is_compressed=metadata_source.is_compressed,
        tags=tags,
        notes="\n".join(notes),
        reference_url=url_source.reference_url,
        warnings=list(dict.fromkeys(warnings)),
    )
    return ArtistResolution(member_indexes=group.member_indexes, canonical=canonical)


def duplicate_signature(value: str) -> str:
    """Public stable signature used to explain likely duplicate matches."""

    return _variant_signature(value)


def _comparison_reasons(left: str, right: str) -> tuple[str, ...]:
    reasons: list[str] = []
    if name_key(left) == name_key(right):
        return ("same name after trimming and case folding",)

    if collapsed_name(left) == collapsed_name(right):
        reasons.append("same name after punctuation and spacing normalization")

    left_without_article = _without_leading_article(left)
    right_without_article = _without_leading_article(right)
    if collapsed_name(left_without_article) == collapsed_name(right_without_article):
        reasons.append("same name after leading-article normalization")

    left_signature = _variant_signature(left)
    right_signature = _variant_signature(right)
    if left_signature and left_signature == right_signature:
        reasons.append("same base artist after known variant suffix normalization")

    left_tokens = _base_tokens(left)
    right_tokens = _base_tokens(right)
    shorter, longer = sorted((left_tokens, right_tokens), key=len)
    if len(shorter) >= 2 and len(longer) == len(shorter) + 1 and longer[: len(shorter)] == shorter:
        reasons.append("one name extends the other's multi-word artist name")
    elif (
        len(shorter) == 1 and len(shorter[0]) >= 6 and len(longer) == 2 and longer[0] == shorter[0]
    ):
        reasons.append("one name extends a distinctive single-word artist name")

    return tuple(dict.fromkeys(reasons))


def _preferred_name_score(artist: ParsedArtist) -> tuple[int, int, int]:
    tokens = _tokens(artist.name)
    base = _base_tokens(artist.name)
    stripped_count = len(tokens) - len(base)
    return stripped_count, len(artist.name), artist.source_line


def _without_leading_article(value: str) -> str:
    return re.sub(r"^\s*the\s+", "", value, flags=re.IGNORECASE)


def _canonical_display_name(value: str) -> str:
    """Remove only recognized trailing labels while preserving source casing."""

    result = value.strip()
    changed = True
    suffixes = sorted(_VARIANT_SUFFIXES, key=len, reverse=True)
    while result and changed:
        changed = False
        for suffix in suffixes:
            expression = r"(?:\s+|[-_]+)" + r"(?:\s+|[-_]+)".join(
                re.escape(token) for token in suffix
            )
            updated = re.sub(expression + r"\s*$", "", result, flags=re.IGNORECASE)
            if updated != result:
                result = updated.rstrip()
                changed = True
                break
        updated = re.sub(
            r"(?:\s+|[-_]+)(?:company|studio)\s*$",
            "",
            result,
            flags=re.IGNORECASE,
        )
        if updated != result:
            result = updated.rstrip()
            changed = True
    return result or value.strip()


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def _base_tokens(value: str) -> tuple[str, ...]:
    tokens = list(_tokens(value))
    if tokens and tokens[0] == "the":
        tokens.pop(0)
    changed = True
    while tokens and changed:
        changed = False
        for suffix in _VARIANT_SUFFIXES:
            if len(tokens) > len(suffix) and tuple(tokens[-len(suffix) :]) == suffix:
                del tokens[-len(suffix) :]
                changed = True
                break
        if tokens and len(tokens) > 1 and tokens[-1] in _ORGANIZATION_SUFFIXES:
            tokens.pop()
            changed = True
    return tuple(tokens)


def _variant_signature(value: str) -> str:
    return "".join(_base_tokens(value))
