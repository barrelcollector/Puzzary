from __future__ import annotations

import random
from collections import Counter
from typing import Any

from app.content import words_for_theme
from app.puzzles import GeneratedPuzzle, PuzzleType, register_puzzle_type


GUARANTEED_CHAINS = [
    ["TIP", "PINT", "POINT", "PISTON", "OPTIONS", "STOOPING"],
]


def normalize_word(value: str) -> str:
    return "".join(ch for ch in str(value).upper() if ch.isalnum())


def typed_tag_values(tags: list[Any], tag_type: str) -> list[str]:
    values = []
    for tag in tags:
        if getattr(tag, "tag_type", "") != tag_type:
            continue
        value = str(getattr(tag, "name", "")).strip()
        if value and value not in values:
            values.append(value)
    return values


def definition_clues(definition: Any) -> list[str]:
    if definition is None:
        return []
    clues = typed_tag_values(getattr(definition, "tags", []), "Clue")
    definition_text = str(getattr(definition, "definition", "")).strip()
    if definition_text:
        clue = f"Definition: {definition_text}"
        if clue not in clues:
            clues.append(clue)
    return clues


def word_clues(word: Any, definition: Any) -> list[str]:
    clues = definition_clues(definition)
    fallback = str(getattr(word, "clue", "") or "").strip()
    if fallback and fallback not in clues:
        clues.append(fallback)
    notes = getattr(word, "properties", {}) or {}
    if isinstance(notes, dict):
        note = str(notes.get("notes", "") or "").strip()
        if note and note not in clues:
            clues.append(note)
    display = str(getattr(word, "display_text", "") or getattr(word, "word", "")).strip()
    if not clues and display:
        clues.append(f"{display.title()} entry.")
    return clues


def build_source(word_entries: Any, difficulty: str, excluded_tag_ids: set[int] | None = None) -> list[dict] | None:
    entries = []
    seen = set()
    for entry in word_entries or []:
        word = entry.get("word") if isinstance(entry, dict) else None
        if word is None:
            continue
        normalized = normalize_word(getattr(word, "word", ""))
        if len(normalized) < 2 or normalized in seen:
            continue
        seen.add(normalized)
        definition = entry.get("definition") if isinstance(entry, dict) else None
        entries.append(
            {
                "word": normalized,
                "display": str(getattr(word, "display_text", "") or normalized),
                "clues": word_clues(word, definition),
                "definition_id": getattr(definition, "id", None) if definition is not None else None,
            }
        )
    return entries or None


def fallback_source(theme: str, difficulty: str) -> list[dict]:
    guaranteed = [word for chain in GUARANTEED_CHAINS for word in chain]
    themed = words_for_theme(theme, difficulty)
    words = []
    for word in themed + guaranteed:
        normalized = normalize_word(word)
        if normalized and normalized not in words:
            words.append(normalized)
    return [
        {
            "word": normalize_word(word),
            "display": str(word).title(),
            "clues": [f"Word from {theme.replace('_', ' ')}."],
            "definition_id": None,
        }
        for word in words
    ]


def extends_by_one_letter(previous: str, candidate: str) -> bool:
    if len(candidate) != len(previous) + 1:
        return False
    previous_counts = Counter(previous)
    candidate_counts = Counter(candidate)
    return all(candidate_counts[letter] >= count for letter, count in previous_counts.items())


def choose_chain(source_data: list[dict], difficulty: str, rng: random.Random) -> list[dict]:
    target_count = {"easy": 4, "medium": 5, "hard": 6}.get(difficulty, 4)
    candidates_by_length: dict[int, list[dict]] = {}
    for entry in source_data:
        word = normalize_word(entry.get("word", ""))
        if len(word) < 2:
            continue
        item = {**entry, "word": word}
        candidates_by_length.setdefault(len(word), []).append(item)

    for options in candidates_by_length.values():
        rng.shuffle(options)

    def extend(chain: list[dict]) -> list[dict] | None:
        if len(chain) == target_count:
            return chain
        previous = chain[-1]["word"]
        next_options = [
            entry
            for entry in candidates_by_length.get(len(previous) + 1, [])
            if extends_by_one_letter(previous, entry["word"])
        ]
        rng.shuffle(next_options)
        for entry in next_options:
            result = extend([*chain, entry])
            if result is not None:
                return result
        return None

    starts = [entry for options in candidates_by_length.values() for entry in options]
    rng.shuffle(starts)
    for entry in starts:
        result = extend([entry])
        if result is not None:
            return result

    best_chain: list[dict] = []

    def collect_best(chain: list[dict]) -> None:
        nonlocal best_chain
        if len(chain) > len(best_chain):
            best_chain = chain
        previous = chain[-1]["word"]
        for entry in candidates_by_length.get(len(previous) + 1, []):
            if extends_by_one_letter(previous, entry["word"]):
                collect_best([*chain, entry])

    for entry in starts:
        collect_best([entry])
    if len(best_chain) >= 3:
        return best_chain
    raise ValueError("Word pyramid generation needs at least three words where each row adds one letter to the previous row.")


def check_answer(answer: str, solution_payload: dict) -> tuple[bool, int, int]:
    expected = [str(row["answer"]).upper() for row in solution_payload.get("rows", [])]
    supplied = [
        normalize_word(part)
        for part in answer.replace("\n", ",").replace(";", ",").split(",")
        if normalize_word(part)
    ]
    if len(supplied) <= 1 and "," not in answer and "\n" not in answer and ";" not in answer:
        supplied = [normalize_word(part) for part in answer.split() if normalize_word(part)]
    matched = sum(1 for index, value in enumerate(supplied) if index < len(expected) and value == expected[index])
    return supplied[: len(expected)] == expected, len(expected), matched


def normalize_progress(payload: Any, solution_payload: dict) -> dict:
    answers = getattr(payload, "answers", {}) if payload is not None else {}
    if not isinstance(answers, dict):
        answers = {}
    expected_rows = solution_payload.get("rows", [])
    normalized = {}
    for index, row in enumerate(expected_rows):
        value = normalize_word(answers.get(str(index), ""))
        if value:
            normalized[str(index)] = value[: len(str(row.get("answer", "")))]
    return {"answers": normalized}


def count_progress(progress_payload: dict, solution_payload: dict) -> tuple[int, int]:
    answers = progress_payload.get("answers", {})
    if not isinstance(answers, dict):
        answers = {}
    rows = solution_payload.get("rows", [])
    found = 0
    for index, row in enumerate(rows):
        if normalize_word(answers.get(str(index), "")) == str(row.get("answer", "")).upper():
            found += 1
    return found, len(rows)


def generate(theme: str, difficulty: str, source_data: list[dict] | None = None, seed: str | None = None) -> GeneratedPuzzle:
    source = source_data if source_data is not None else fallback_source(theme, difficulty)
    generation_seed = seed or f"word_pyramid:{theme}:{difficulty}:{','.join(str(item.get('word', '')) for item in source)}"
    rng = random.Random(generation_seed)
    try:
        chain = choose_chain(source, difficulty, rng)
    except ValueError:
        if source_data is None:
            raise
        chain = choose_chain([*source, *fallback_source(theme, difficulty)], difficulty, rng)

    rows = []
    solution_rows = []
    for index, entry in enumerate(chain):
        answer = normalize_word(entry.get("word", ""))
        clues = [str(clue).strip() for clue in entry.get("clues", []) if str(clue).strip()]
        primary = clues[0] if clues else f"{answer.title()} entry."
        row = {
            "index": index,
            "number": index + 1,
            "length": len(answer),
            "clue": primary,
            "clues": clues or [primary],
            "definition_id": entry.get("definition_id"),
        }
        rows.append(row)
        solution_rows.append({**row, "answer": answer})

    prompt = "Fill each row. Every answer is an anagram of the row above it plus one new letter."
    play_payload = {"kind": "word_pyramid", "rows": rows}
    print_payload = {"kind": "word_pyramid", "rows": rows}
    solution_payload = {"rows": solution_rows}
    return GeneratedPuzzle(
        title=f"{theme.replace('_', ' ').title()} Word Pyramid",
        prompt=prompt,
        print_payload=print_payload,
        play_payload=play_payload,
        solution_payload=solution_payload,
        hints=[
            f"The pyramid has {len(rows)} rows.",
            "Each answer uses every letter from the row above, plus one new letter.",
            f"The shortest answer has {rows[0]['length']} letters.",
        ],
        metadata={"generator": "local.word_pyramid.v1", "seed": generation_seed, "row_count": len(rows)},
    )


register_puzzle_type(
    PuzzleType(
        slug="word_pyramid",
        label="Word pyramid",
        generator=generate,
        source_builder=build_source,
        answer_checker=check_answer,
        progress_normalizer=normalize_progress,
        progress_counter=count_progress,
        description="A clue-driven chain where each answer grows by one letter.",
        supports_print=True,
        supports_web_play=True,
        source_requirements=("word_bank", "definitions"),
    )
)
