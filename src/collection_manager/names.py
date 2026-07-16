from __future__ import annotations

import re
import unicodedata


def display_name(value: str) -> str:
    return " ".join(value.strip().split())


def name_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", display_name(value))
    return normalized.casefold()


def tag_key(value: str) -> str:
    return name_key(value)


def collapsed_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^a-z0-9]+", "", value)
