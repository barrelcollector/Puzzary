"""Bounded relationship loading and aggregate usage for large dictionaries."""
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models import (
    DictionaryDefinition, DictionaryWord, dictionary_definition_tag_links,
    dictionary_word_tag_links, puzzle_tag_links, word_bank_tag_links,
)


def dictionary_statement():
    return select(DictionaryWord).options(
        selectinload(DictionaryWord.tags),
        selectinload(DictionaryWord.bank_links),
        selectinload(DictionaryWord.definitions).selectinload(DictionaryDefinition.tags),
    ).order_by(DictionaryWord.word)


def tag_usage_counts(db, tags):
    usage = {tag.id: {"puzzles": 0, "word_banks": 0, "words": 0, "definitions": 0} for tag in tags}
    for name, links in (("puzzles", puzzle_tag_links), ("word_banks", word_bank_tag_links),
                        ("words", dictionary_word_tag_links), ("definitions", dictionary_definition_tag_links)):
        for tag_id, count in db.execute(select(links.c.tag_id, func.count()).group_by(links.c.tag_id)):
            if tag_id in usage:
                usage[tag_id][name] = count
    for counts in usage.values():
        counts["total"] = sum(counts.values())
    return usage
