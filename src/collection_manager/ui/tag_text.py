from __future__ import annotations

import json
from collections.abc import Iterable


def format_tag_text(tags: Iterable[str]) -> str:
    """Render tags as editable comma-separated tokens with JSON quoting when needed."""

    rendered: list[str] = []
    for value in tags:
        tag = str(value)
        if _needs_quotes(tag):
            rendered.append(json.dumps(tag, ensure_ascii=False))
        else:
            rendered.append(tag)
    return ", ".join(rendered)


def parse_tag_text(value: str) -> list[str]:
    """Parse bare or JSON-quoted tag tokens without splitting commas inside quotes."""

    if not value.strip():
        return []
    raw_tokens: list[str] = []
    token: list[str] = []
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            token.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            token.append(character)
        elif character == ",":
            raw_tokens.append("".join(token).strip())
            token.clear()
        else:
            token.append(character)
    if in_string:
        raise ValueError("A quoted tag is missing its closing quote.")
    raw_tokens.append("".join(token).strip())

    tags: list[str] = []
    seen: set[str] = set()
    for raw in raw_tokens:
        if not raw:
            continue
        if raw.startswith('"'):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid quoted tag: {raw}") from exc
            if not isinstance(decoded, str):
                raise ValueError("Quoted tags must contain JSON strings.")
            tag = decoded
        elif '"' in raw:
            raise ValueError(f"Quote the complete tag token: {raw}")
        else:
            tag = raw
        tag = " ".join(tag.strip().split())
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            tags.append(tag)
    return tags


def _needs_quotes(value: str) -> bool:
    return (
        value != value.strip() or not value or any(character in value for character in ',"\\\r\n')
    )
