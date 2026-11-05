from __future__ import annotations

import re
from typing import Any, Iterable


GENERIC_CLUE_PATTERNS = (
    re.compile(r"^.+ entry\.?$", re.IGNORECASE),
    re.compile(r"^word from .+\.?$", re.IGNORECASE),
    re.compile(r"^.+ term starting with .+ \(\d+ letters\)\.?$", re.IGNORECASE),
)


def _texts(values: Any) -> list[str]:
    if isinstance(values, str):
        values = values.splitlines()
    if not isinstance(values, (list, tuple)):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def unique_clues(values: Iterable[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        clue = str(value or "").strip()
        key = " ".join(clue.casefold().split())
        if clue and key not in seen:
            result.append(clue)
            seen.add(key)
    return result


def typed_tag_values(tags: Iterable[Any], tag_type: str) -> list[str]:
    return unique_clues(
        str(getattr(tag, "name", "")).strip()
        for tag in tags or []
        if getattr(tag, "tag_type", "") == tag_type
    )


def stored_definition_clues(definition: Any) -> list[str]:
    """Return complete authored clue text, preferring lossless JSON over short tags."""
    if definition is None:
        return []
    properties = getattr(definition, "properties", {}) or {}
    property_clues = _texts(properties.get("clues", [])) if isinstance(properties, dict) else []
    singular = str(properties.get("clue", "")).strip() if isinstance(properties, dict) else ""
    return unique_clues([*property_clues, singular, *typed_tag_values(getattr(definition, "tags", []), "Clue")])


def definition_clue_tiers(definition: Any) -> list[str]:
    """Build progressive tiers from authored hints followed by the full definition."""
    if definition is None or getattr(definition, "archived_at", None) is not None:
        return []
    definition_text = str(getattr(definition, "definition", "") or "").strip()
    clues = [
        clue for clue in stored_definition_clues(definition)
        if not is_generic_clue(clue) and not is_truncated_definition_preview(clue, definition_text)
    ]
    if definition_text:
        clues.append(f"Definition: {definition_text}")
    return unique_clues(clues)


def is_generic_clue(value: str) -> bool:
    text = " ".join(str(value or "").split())
    return any(pattern.fullmatch(text) for pattern in GENERIC_CLUE_PATTERNS)


def is_truncated_definition_preview(clue: str, definition: str) -> bool:
    """Recognize old enrichment previews so the complete definition can replace them."""
    text = " ".join(str(clue or "").split())
    if not text.endswith("..."):
        return False
    prefix = text[:-3].rstrip(" ,").casefold()
    complete = " ".join(str(definition or "").split()).casefold()
    without_article = re.sub(r"^(a|an|the)\s+", "", complete)
    return bool(prefix) and (complete.startswith(prefix) or without_article.startswith(prefix))


def word_clue_tiers(
    word: Any,
    definitions: Iterable[Any] | None = None,
    *,
    include_word_clue: bool = True,
) -> list[str]:
    """Shared tier request used by clue-driven puzzle modules."""
    selected = list(definitions) if definitions is not None else sorted(
        getattr(word, "definitions", []) or [], key=lambda item: getattr(item, "position", 0)
    )
    clues = []
    for definition in selected:
        clues.extend(definition_clue_tiers(definition))
    if include_word_clue:
        legacy = str(getattr(word, "clue", "") or "").strip()
        if legacy and not is_generic_clue(legacy):
            clues.append(legacy)
    return unique_clues(clues)


def properties_with_clues(properties: dict | None, clues: Iterable[str]) -> dict:
    result = dict(properties or {})
    complete = unique_clues(clues)
    if complete:
        result["clues"] = complete
    else:
        result.pop("clues", None)
        result.pop("clue", None)
    return result
