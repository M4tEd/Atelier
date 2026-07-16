from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from collection_manager.constants import HeavyStatus, SizeQualifier, Tier
from collection_manager.domain import ImportPreview, ParsedArtist
from collection_manager.names import display_name, tag_key


class ParseError(ValueError):
    """A source line cannot be represented as an artist record."""


_HEADER_LOOKUP = {" ".join(tier.value.casefold().split()): tier for tier in Tier}
_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_SIZE_RE = re.compile(
    r"""
    ^\s*
    (?P<approx>~|about\s+|approx(?:imately)?\.?\s+)?
    (?P<value>\d+(?:\.\d+)?)\s*
    (?P<plus_before>\+)?\s*
    (?P<unit>b|[kmgt]i?b)\s*
    (?P<plus_after>\+)?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BRACKET_NOTE_RE = re.compile(r"^(?:vol(?:ume)?\.?\s*\d|parts?\s+\d)", re.IGNORECASE)
_LABEL_RE = re.compile(r"^(points|size|note|url|compressed|heavy)\s*:\s*(.*)$", re.I | re.S)


def tier_from_header(value: str) -> Tier | None:
    """Return a tier for an exact, case-insensitive section heading."""

    return _HEADER_LOOKUP.get(" ".join(value.strip().casefold().split()))


def preview_import(path: Path | str) -> ImportPreview:
    """Read and parse a legacy or canonical UTF-8 collection file."""

    source_path = Path(path).expanduser().resolve()
    raw = source_path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError(f"{source_path} is not valid UTF-8") from exc
    return parse_text(
        text,
        source_path=source_path,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def parse_text(
    text: str,
    *,
    source_path: Path | str = Path("<memory>"),
    sha256: str | None = None,
) -> ImportPreview:
    """Parse a complete tiered text document without stopping at bad lines."""

    from collection_manager.duplicates import detect_duplicate_groups

    artists: list[ParsedArtist] = []
    unparseable: list[tuple[int, str, str]] = []
    global_warnings: list[str] = []
    current_tier: Tier | None = None
    seen_tiers: set[Tier] = set()

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip().lstrip("\ufeff")
        if not stripped:
            continue
        header = tier_from_header(stripped)
        if header is not None:
            current_tier = header
            seen_tiers.add(header)
            continue
        if current_tier is None:
            unparseable.append((line_number, raw_line, "Artist appears before a tier heading"))
            continue
        try:
            artists.append(parse_artist_line(raw_line, current_tier, line_number))
        except (ParseError, InvalidOperation, ValueError) as exc:
            unparseable.append((line_number, raw_line, str(exc)))

    missing = [tier.value for tier in Tier if tier not in seen_tiers]
    if missing:
        global_warnings.append(f"Missing tier sections: {', '.join(missing)}")
    if unparseable:
        global_warnings.append(f"{len(unparseable)} source line(s) could not be parsed")

    duplicate_groups = detect_duplicate_groups(artists)
    if duplicate_groups:
        global_warnings.append(
            f"{len(duplicate_groups)} possible duplicate artist group(s) require resolution"
        )

    source = Path(source_path)
    digest = sha256 or hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ImportPreview(
        source_path=source,
        sha256=digest,
        artists=artists,
        duplicate_groups=duplicate_groups,
        global_warnings=global_warnings,
        unparseable=unparseable,
    )


def parse_artist_line(raw_line: str, tier: Tier, line_number: int = 1) -> ParsedArtist:
    """Parse one entry line from either supported text format."""

    line = raw_line.strip()
    if not line:
        raise ParseError("Artist line is empty")
    name, groups, stray_text = _extract_groups(line)
    name = display_name(name)
    if not name:
        raise ParseError("Artist name is missing")

    points = 0
    points_seen = False
    last_updated = None
    size_value: Decimal | None = None
    size_unit: str | None = None
    size_qualifier: SizeQualifier | None = None
    heavy_status = HeavyStatus.NO
    explicit_heavy: HeavyStatus | None = None
    is_compressed = False
    tags: list[str] = []
    note_parts: list[str] = []
    reference_url: str | None = None
    warnings: list[str] = []

    if stray_text:
        note_parts.append(stray_text)
        warnings.append("Text outside metadata groups was preserved as a note")

    for delimiter, content in groups:
        content = content.strip()
        if delimiter == "[":
            parsed_date = _parse_date(content)
            if parsed_date is not None:
                if last_updated is not None:
                    warnings.append("Multiple dates found; the latest date was kept")
                    last_updated = max(last_updated, parsed_date)
                else:
                    last_updated = parsed_date
            elif _is_url(content):
                if reference_url is not None and reference_url != content:
                    warnings.append("Multiple URLs found; the last URL was kept")
                reference_url = content
            elif _BRACKET_NOTE_RE.match(content):
                note_parts.append(content)
                warnings.append("Bracketed volume/part text was preserved as a note")
            elif content:
                tags.extend(_parse_tag_group(content))
            continue

        if not content:
            continue
        labeled = _LABEL_RE.match(content)
        if labeled:
            label = labeled.group(1).casefold()
            value = labeled.group(2).strip()
            if label == "points":
                if not _INTEGER_RE.fullmatch(value):
                    raise ParseError(f"Invalid labeled point value: {value!r}")
                if points_seen:
                    warnings.append("Multiple point values found; the last value was kept")
                points = int(value)
                points_seen = True
            elif label == "size":
                parsed_size = _parse_size(value)
                if parsed_size is None:
                    raise ParseError(f"Invalid labeled size value: {value!r}")
                size_value, size_unit, size_qualifier = parsed_size
            elif label == "note":
                decoded = _decode_json_string(value, "note")
                if decoded:
                    note_parts.append(decoded)
            elif label == "url":
                decoded = _decode_json_string(value, "URL")
                reference_url = decoded or None
            elif label == "compressed":
                is_compressed = _parse_boolean(value, "compressed")
            elif label == "heavy":
                try:
                    explicit_heavy = HeavyStatus(value.strip().strip('"').casefold())
                except ValueError as exc:
                    raise ParseError(f"Invalid heavy status: {value!r}") from exc
            continue

        for part in _split_top_level_commas(content):
            part = part.strip()
            if not part:
                continue
            if _INTEGER_RE.fullmatch(part):
                if points_seen:
                    warnings.append("Multiple point values found; the last value was kept")
                points = int(part)
                points_seen = True
                continue
            parsed_size = _parse_size(part)
            if parsed_size is not None:
                if size_value is not None:
                    warnings.append("Multiple sizes found; the last size was kept")
                size_value, size_unit, size_qualifier = parsed_size
                continue
            if re.search(r"\b[kmgt]i?b\b", part, re.IGNORECASE):
                warnings.append(f"Possible size needs review: {part}")
            note_parts.append(_decode_optional_json_string(part))

    notes = ", ".join(part.strip() for part in note_parts if part.strip())
    if size_value is not None and size_unit is not None and size_qualifier is not None:
        heavy_status = infer_heavy_status(size_value, size_unit, size_qualifier)
        if heavy_status is HeavyStatus.UNKNOWN:
            warnings.append("Size does not determine whether the 5 GB heavy threshold is met")
    if re.search(r"\bheav(?:y|ier|iest)\b", notes, re.IGNORECASE):
        warnings.append("A note mentions heavy files; confirm heavy status in the import preview")
        if size_value is None and explicit_heavy is None:
            heavy_status = HeavyStatus.UNKNOWN
    if explicit_heavy is not None:
        heavy_status = explicit_heavy

    return ParsedArtist(
        source_line=line_number,
        raw_line=raw_line,
        tier=tier,
        name=name,
        points=points,
        last_updated=last_updated,
        size_value=size_value,
        size_unit=size_unit,
        size_qualifier=size_qualifier,
        heavy_status=heavy_status,
        is_compressed=is_compressed,
        tags=_deduplicate_tags(tags),
        notes=notes,
        reference_url=reference_url,
        warnings=_deduplicate_strings(warnings),
    )


def infer_heavy_status(value: Decimal, unit: str, qualifier: SizeQualifier) -> HeavyStatus:
    """Infer the 5 GB threshold without treating ambiguous lower bounds as facts."""

    normalized_unit = unit.upper()
    bytes_per_gb = Decimal(1_000_000_000)
    factors = {
        "B": Decimal(1) / bytes_per_gb,
        "KB": Decimal("0.000001"),
        "MB": Decimal("0.001"),
        "GB": Decimal("1"),
        "TB": Decimal("1000"),
        "KIB": Decimal(1024) / bytes_per_gb,
        "MIB": Decimal(1024**2) / bytes_per_gb,
        "GIB": Decimal(1024**3) / bytes_per_gb,
        "TIB": Decimal(1024**4) / bytes_per_gb,
    }
    try:
        factor = factors[normalized_unit]
    except KeyError as exc:
        raise ValueError(f"Unsupported size unit: {unit!r}") from exc
    value_in_gb = value * factor
    if qualifier is SizeQualifier.APPROXIMATE:
        return HeavyStatus.UNKNOWN
    if qualifier is SizeQualifier.AT_LEAST and value_in_gb < Decimal(5):
        return HeavyStatus.UNKNOWN
    return HeavyStatus.YES if value_in_gb >= Decimal(5) else HeavyStatus.NO


def _extract_groups(line: str) -> tuple[str, list[tuple[str, str]], str]:
    if line.startswith('"'):
        try:
            decoded_name, end = json.JSONDecoder().raw_decode(line)
        except json.JSONDecodeError as exc:
            raise ParseError("Invalid JSON-escaped artist name") from exc
        if not isinstance(decoded_name, str):
            raise ParseError("Canonical artist name must be a JSON string")
        groups, stray = _scan_groups(line, end)
        return decoded_name, groups, stray

    first_group = min(
        (index for index in (line.find("["), line.find("(")) if index >= 0),
        default=-1,
    )
    if first_group < 0:
        return line, [], ""

    name = line[:first_group]
    groups, stray = _scan_groups(line, first_group)
    return name, groups, stray


def _scan_groups(line: str, start: int) -> tuple[list[tuple[str, str]], str]:
    groups: list[tuple[str, str]] = []
    stray: list[str] = []
    index = start
    while index < len(line):
        if line[index].isspace():
            index += 1
            continue
        opening = line[index]
        if opening not in "[(":
            start = index
            while index < len(line) and line[index] not in "[(":
                index += 1
            value = line[start:index].strip()
            if value:
                stray.append(value)
            continue
        closing = "]" if opening == "[" else ")"
        end = _find_group_end(line, index + 1, closing)
        if end is None:
            raise ParseError(f"Unclosed {opening!r} metadata group")
        groups.append((opening, line[index + 1 : end]))
        index = end + 1
    return groups, " ".join(stray)


def _find_group_end(line: str, start: int, closing: str) -> int | None:
    quoted = False
    escaped = False
    for index in range(start, len(line)):
        character = line[index]
        if escaped:
            escaped = False
            continue
        if quoted and character == "\\":
            escaped = True
            continue
        if character == '"':
            quoted = not quoted
            continue
        if character == closing and not quoted:
            return index
    return None


def _parse_date(value: str):  # noqa: ANN202 - inferred date | None is clearest at call site
    for pattern in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            continue
    return None


def _parse_size(value: str) -> tuple[Decimal, str, SizeQualifier] | None:
    match = _SIZE_RE.fullmatch(value)
    if match is None:
        return None
    qualifier = SizeQualifier.EXACT
    if match.group("approx"):
        qualifier = SizeQualifier.APPROXIMATE
    elif match.group("plus_before") or match.group("plus_after"):
        qualifier = SizeQualifier.AT_LEAST
    return Decimal(match.group("value")), match.group("unit").upper(), qualifier


def _parse_tag_group(value: str) -> list[str]:
    tags: list[str] = []
    for item in _split_top_level_commas(value):
        item = item.strip()
        if not item:
            continue
        tags.append(_decode_optional_json_string(item))
    return tags


def _split_top_level_commas(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quoted = False
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quoted and character == "\\":
            escaped = True
            continue
        if character == '"':
            quoted = not quoted
        elif character == "," and not quoted:
            parts.append(value[start:index])
            start = index + 1
    if quoted:
        raise ParseError("Unclosed JSON string in metadata")
    parts.append(value[start:])
    return parts


def _decode_json_string(value: str, label: str) -> str:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ParseError(f"Invalid JSON-escaped {label}") from exc
    if not isinstance(decoded, str):
        raise ParseError(f"Labeled {label} must be a JSON string")
    return decoded


def _decode_optional_json_string(value: str) -> str:
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return _decode_json_string(value, "text")
    return value


def _parse_boolean(value: str, label: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"true", "yes", "1"}:
        return True
    if normalized in {"false", "no", "0"}:
        return False
    raise ParseError(f"Invalid {label} boolean: {value!r}")


def _is_url(value: str) -> bool:
    return bool(re.match(r"^(?:https?|file)://", value.strip(), re.IGNORECASE))


def _deduplicate_tags(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(value.strip().split())
        key = tag_key(cleaned)
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def _deduplicate_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
