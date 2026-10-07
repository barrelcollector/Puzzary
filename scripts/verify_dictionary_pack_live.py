"""Read-only generation checks against the installed content pack."""
from collections import Counter
import json
import time
from app.database import SessionLocal
from app.dictionary_queries import dictionary_statement
from app.puzzles import get_puzzle_type

with SessionLocal() as db:
    words = list(db.scalars(dictionary_statement()))
    entries = [{"word": word, "definition": definition,
                "properties": {**(word.properties or {}), **(definition.properties or {})}}
               for word in words if word.archived_at is None and word.banned_at is None
               for definition in word.definitions if definition.archived_at is None
               and (definition.properties or {}).get("content_pack") == "english-wordnet-5000-v1"]
    assert len({entry["word"].word.lower() for entry in entries}) >= 4900
    results = []
    for kind, options in (("word_search", {"grid_size": "14", "word_count_min": "10", "word_count_limit": "10"}),
                          ("crossword", {"grid_size": "14", "answer_count_min": "10", "answer_count": "10"}),
                          ("word_pyramid", {"row_count": "5"}), ("spell_it", {})):
        plugin = get_puzzle_type(kind)
        start = time.monotonic()
        source = plugin.source_builder(entries, "easy", set())
        generated = plugin.generator("expanded-english", "easy", source, "dictionary-expansion-live-check", options)
        assert generated.play_payload and generated.solution_payload
        if kind == "word_search":
            assert generated.metadata["generation_options"]["word_count"] == 10
        if kind == "crossword":
            assert len(generated.solution_payload["entries"]) == 10
        results.append({"kind": kind, "source_entries": len(source), "seconds": round(time.monotonic() - start, 2)})
    pyramid = get_puzzle_type("word_pyramid")
    source = pyramid.source_builder(entries, "easy", set())
    chains = set()
    for index in range(5):
        generated = pyramid.generator("expanded-english", "easy", source, f"dictionary-pyramid-variety-{index}", {"row_count": "5"})
        rows = generated.solution_payload["rows"]
        answers = tuple(row["answer"] for row in rows)
        assert len(answers) == 5 and all(row.get("definition_id") is not None for row in rows)
        for first, second in zip(answers, answers[1:]):
            assert len(second) == len(first) + 1 and not (Counter(first) - Counter(second))
        chains.add(answers)
    assert len(chains) > 1, "Pyramid source still produces one chain"
    print(json.dumps({"generators": results, "distinct_five_row_pyramids": len(chains), "production_puzzles_created": 0}, indent=2))
