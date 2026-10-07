"""Large-library query counts and tag-usage parity in an isolated database."""
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from app.database import Base
from app.dictionary_queries import dictionary_statement, tag_usage_counts, word_bank_entry_counts
from app.models import DictionaryDefinition, DictionaryWord, Puzzle, PuzzleTag, WordBank, WordBankWord

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
with Session(engine) as db:
    shared = PuzzleTag(name="Shared", tag_type="Topic", slug="topic-shared")
    unused = PuzzleTag(name="Unused", tag_type="Topic", slug="topic-unused")
    db.add_all([shared, unused])
    puzzle = Puzzle(id="query-test", puzzle_type="word_search", title="Test", theme="test", difficulty="easy",
                    print_payload={}, play_payload={}, solution_payload={}, tags=[shared])
    bank = WordBank(name="Test", slug="test", tags=[shared], payload={"mode": "manual"})
    db.add_all([puzzle, bank])
    for index in range(600):
        unique = PuzzleTag(name=f"Clue {index}", tag_type="Clue", slug=f"clue-{index}")
        word = DictionaryWord(word=f"word{index}", display_text=f"Word {index}", tags=[shared])
        definition = DictionaryDefinition(word=word, definition=f"Definition {index}", tags=[shared, unique])
        bank.word_links.append(WordBankWord(word=word, definition=definition, position=index))
        db.add_all([word, definition])
    db.commit()
    automatic = WordBank(name="Automatic", slug="automatic", payload={"mode": "automatic"}, tags=[shared])
    db.add(automatic)
    db.commit()

queries = []
@event.listens_for(engine, "before_cursor_execute")
def track(conn, cursor, statement, parameters, context, many):
    queries.append(statement)

with Session(engine) as db:
    banks = list(db.scalars(select(WordBank)))
    queries.clear()
    counts = word_bank_entry_counts(db, banks)
    assert len(queries) == 2
    assert all(counts[bank.id] == 600 for bank in banks)
    tags = list(db.scalars(select(PuzzleTag)))
    queries.clear()
    usage = tag_usage_counts(db, tags)
    assert len(queries) == 4, f"Tag usage issued {len(queries)} queries"
    shared = next(t for t in tags if t.name == "Shared")
    unused = next(t for t in tags if t.name == "Unused")
    assert usage[shared.id] == {"puzzles": 1, "word_banks": 2, "words": 600, "definitions": 600, "total": 1203}
    assert usage[unused.id]["total"] == 0
    for tag in tags:
        assert usage[tag.id]["total"] == len(tag.puzzles) + len(tag.word_banks) + len(tag.dictionary_words) + len(tag.dictionary_definitions)
with Session(engine) as db:
    queries.clear()
    words = list(db.scalars(dictionary_statement()))
    load_queries = len(queries)
    for word in words:
        assert word.tags and word.bank_links and word.definitions
        assert word.definitions[0].tags
    assert len(queries) == load_queries, "Relationship iteration issued lazy queries"
    assert load_queries <= 10, f"Dictionary issued {load_queries} queries"
with Session(engine) as db:
    words = list(db.scalars(dictionary_statement()))
    words[0].banned_at = datetime.now(timezone.utc)
    words[1].archived_at = datetime.now(timezone.utc)
    words[2].definitions[0].archived_at = datetime.now(timezone.utc)
    extra = DictionaryDefinition(word=words[3], definition="Another meaning", tags=list(words[3].tags))
    db.add(extra)
    empty = WordBank(name="Empty", slug="empty", payload={"mode": "automatic"})
    db.add(empty)
    db.commit()
    banks = list(db.scalars(select(WordBank)))
    counts = word_bank_entry_counts(db, banks)
    by_slug = {bank.slug: counts[bank.id] for bank in banks}
    assert by_slug == {"test": 597, "automatic": 599, "empty": 0}, by_slug
print("PASS: aggregate usage parity, four usage queries, batched dictionary loading, no per-word lazy queries")
