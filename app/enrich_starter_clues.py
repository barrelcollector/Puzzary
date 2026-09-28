from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from hashlib import sha1

from sqlalchemy import select

from app.database import SessionLocal
from app.models import DictionaryDefinition, DictionaryWord, PuzzleTag
from app.seed_starter_library import BANKS, MULTIPLE_DEFINITIONS, SPECIFIC_DEFINITIONS, slugify


PARTS_OF_SPEECH = {
    "n": "Noun",
    "v": "Verb",
    "adj": "Adjective",
    "adv": "Adverb",
}


def get_or_create_tag(db, tag_type: str, name: str) -> PuzzleTag | None:
    tag_type = tag_type.strip() or "General"
    name = name.strip()
    if not name:
        return None
    tag = db.scalar(select(PuzzleTag).where(PuzzleTag.tag_type == tag_type, PuzzleTag.name == name))
    if tag is not None:
        return tag
    slug_base = slugify(f"{tag_type}-{name}")
    slug = slug_base if len(slug_base) <= 80 else f"{slug_base[:69]}-{sha1(slug_base.encode()).hexdigest()[:10]}"
    tag = PuzzleTag(tag_type=tag_type, name=name, slug=slug)
    db.add(tag)
    db.flush()
    return tag


def datamuse_definition(word: str) -> tuple[str, str] | None:
    url = "https://api.datamuse.com/words?" + urllib.parse.urlencode({"sp": word, "md": "d", "max": "1"})
    with urllib.request.urlopen(url, timeout=12) as response:
        rows = json.loads(response.read().decode("utf-8"))
    if not rows or not rows[0].get("defs"):
        return None
    for raw_definition in rows[0]["defs"]:
        if "\t" not in raw_definition:
            continue
        part_code, text = raw_definition.split("\t", 1)
        part_of_speech = PARTS_OF_SPEECH.get(part_code.strip())
        text = clean_definition(text)
        if part_of_speech and text:
            return part_of_speech, text
    return None


def clean_definition(value: str) -> str:
    text = re.sub(r"\([^)]*\)", "", value)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.split(";")[0].strip()
    if len(text) > 150:
        text = text[:147].rsplit(" ", 1)[0].rstrip(",") + "..."
    return text


def clue_from_definition(definition: str) -> str:
    clue = definition.strip()
    clue = re.sub(r"^(a|an|the)\s+", "", clue, flags=re.IGNORECASE)
    clue = clue[:1].upper() + clue[1:] if clue else clue
    if len(clue) > 74:
        clue = clue[:71].rsplit(" ", 1)[0].rstrip(",") + "..."
    return clue


def starter_theme_for_word(word: str) -> str:
    for bank_name, words in BANKS.items():
        if word in words:
            return bank_name
    return "Starter Library"


def update_definition_tags(db, definition: DictionaryDefinition, clue: str, theme: str) -> None:
    tags = []
    for tag in (
        get_or_create_tag(db, "Part of Speech", definition.part_of_speech),
        get_or_create_tag(db, "Clue", clue),
        get_or_create_tag(db, "Theme", theme),
    ):
        if tag is not None:
            tags.append(tag)
    definition.tags = tags


def enrich() -> None:
    custom_words = set(MULTIPLE_DEFINITIONS) | set(SPECIFIC_DEFINITIONS)
    updated = 0
    skipped = 0
    with SessionLocal() as db:
        words = db.scalars(select(DictionaryWord).order_by(DictionaryWord.word)).all()
        for word in words:
            normalized = str(word.word).lower()
            if normalized in custom_words:
                skipped += 1
                continue
            theme = starter_theme_for_word(normalized)
            if theme == "Starter Library":
                skipped += 1
                continue
            try:
                result = datamuse_definition(normalized)
            except Exception as error:  # pragma: no cover - command-line resilience
                print(f"skip {normalized}: {error}")
                skipped += 1
                continue
            if result is None:
                skipped += 1
                continue
            part_of_speech, definition_text = result
            clue = clue_from_definition(definition_text)
            definition = next(
                (
                    item
                    for item in word.definitions
                    if item.source == "puzzary_starter_library"
                    and (item.properties or {}).get("starter_bank") == theme
                ),
                None,
            )
            if definition is None:
                definition = DictionaryDefinition(
                    word=word,
                    definition=definition_text,
                    part_of_speech=part_of_speech,
                    source="puzzary_starter_library",
                    position=1,
                    properties={"starter_bank": theme},
                )
                db.add(definition)
                db.flush()
            definition.definition = definition_text
            definition.part_of_speech = part_of_speech
            definition.properties = {**(definition.properties or {}), "clue": clue, "starter_bank": theme}
            update_definition_tags(db, definition, clue, theme)
            word.clue = clue
            word.part_of_speech = word.part_of_speech or part_of_speech
            updated += 1
            if updated % 25 == 0:
                db.commit()
            time.sleep(0.04)
        db.commit()
    print(f"Starter clue enrichment complete: {updated} updated, {skipped} skipped.")


if __name__ == "__main__":
    enrich()
