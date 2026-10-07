"""Bounded relationship loading and aggregate usage for large dictionaries."""
from sqlalchemy import and_, func, select, union
from sqlalchemy.orm import defer, selectinload

from app.models import (
    DictionaryDefinition, DictionaryWord, WordBankWord, dictionary_definition_tag_links,
    dictionary_word_tag_links, puzzle_tag_links, word_bank_tag_links,
)


def dictionary_statement(include_properties=True):
    definitions = selectinload(DictionaryWord.definitions)
    if not include_properties:
        definitions = definitions.defer(DictionaryDefinition.properties)
    return select(DictionaryWord).options(
        *([] if include_properties else [defer(DictionaryWord.properties)]),
        selectinload(DictionaryWord.tags),
        selectinload(DictionaryWord.bank_links),
        definitions.selectinload(DictionaryDefinition.tags),
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


def word_bank_entry_counts(db, banks):
    """Count resolved senses without materializing dictionary JSON or clues."""
    w, d = DictionaryWord.__table__, DictionaryDefinition.__table__
    bt, wt, dt = word_bank_tag_links, dictionary_word_tag_links, dictionary_definition_tag_links
    active = and_(w.c.archived_at.is_(None), w.c.banned_at.is_(None))
    by_sense = select(bt.c.word_bank_id, w.c.id.label("word_id"), d.c.id.label("definition_id")).select_from(
        bt.join(dt, bt.c.tag_id == dt.c.tag_id).join(d, d.c.id == dt.c.definition_id).join(w, w.c.id == d.c.word_id)
    ).where(active, d.c.archived_at.is_(None))
    by_word = select(bt.c.word_bank_id, w.c.id.label("word_id"), d.c.id.label("definition_id")).select_from(
        bt.join(wt, bt.c.tag_id == wt.c.tag_id).join(w, w.c.id == wt.c.word_id).outerjoin(
            d, and_(d.c.word_id == w.c.id, d.c.archived_at.is_(None)))
    ).where(active)
    matches = union(by_sense, by_word).subquery()
    automatic = dict(db.execute(select(matches.c.word_bank_id, func.count()).group_by(matches.c.word_bank_id)).all())
    links = WordBankWord.__table__
    manual = dict(db.execute(select(links.c.word_bank_id, func.count()).select_from(
        links.join(w, w.c.id == links.c.word_id).outerjoin(d, d.c.id == links.c.definition_id)
    ).where(active, d.c.archived_at.is_(None)).group_by(links.c.word_bank_id)).all())
    return {bank.id: (automatic if (bank.payload or {}).get("mode") == "automatic" else manual).get(bank.id, 0)
            for bank in banks}
