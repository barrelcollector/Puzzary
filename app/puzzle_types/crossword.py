from __future__ import annotations

import random
from typing import Any

from app.content import words_for_theme
from app.puzzles import GeneratedPuzzle, PuzzleType, normalize_answer, register_puzzle_type


def word_allowed(word: Any, excluded_tag_ids: set[int] | None) -> bool:
    if getattr(word, "archived_at", None) is not None or getattr(word, "banned_at", None) is not None:
        return False
    if excluded_tag_ids and any(getattr(tag, "id", None) in excluded_tag_ids for tag in getattr(word, "tags", [])):
        return False
    return True


def definition_allowed(definition: Any, excluded_tag_ids: set[int] | None) -> bool:
    text = str(getattr(definition, "definition", "")).strip()
    if not text:
        return False
    if excluded_tag_ids and any(getattr(tag, "id", None) in excluded_tag_ids for tag in getattr(definition, "tags", [])):
        return False
    return True


def clue_for_word(word: Any, excluded_tag_ids: set[int] | None) -> tuple[str, int | None] | None:
    definitions = sorted(getattr(word, "definitions", []), key=lambda item: item.position)
    for definition in definitions:
        if definition_allowed(definition, excluded_tag_ids):
            return definition.definition, definition.id
    clue = str(getattr(word, "clue", "") or "").strip()
    if clue:
        return clue, None
    return f"{word.display_text.title()} entry.", None


def build_source(word_bank: Any, difficulty: str, excluded_tag_ids: set[int] | None = None) -> list[dict] | None:
    if word_bank is None:
        return None
    count = {"easy": 5, "medium": 7, "hard": 9}.get(difficulty, 5)
    entries = []
    for link in sorted(word_bank.word_links, key=lambda item: item.position):
        if not word_allowed(link.word, excluded_tag_ids):
            continue
        clue = clue_for_word(link.word, excluded_tag_ids)
        if clue is None:
            continue
        clue_text, definition_id = clue
        entries.append(
            {
                "word": link.word.word,
                "clue": clue_text,
                "definition_id": definition_id,
            }
        )
        if len(entries) >= count:
            break
    return entries or None


def check_answer(answer: str, solution_payload: dict) -> tuple[bool, int, int]:
    expected = {str(entry["answer"]).upper() for entry in solution_payload.get("entries", [])}
    supplied = normalize_answer(answer)
    matched = len(expected & supplied)
    return expected <= supplied, len(expected), matched


def normalize_progress(payload: Any, solution_payload: dict) -> dict:
    answers = getattr(payload, "answers", {}) if payload is not None else {}
    if not isinstance(answers, dict):
        answers = {}
    return {
        "answers": {
            str(key): str(value).strip().upper()[:1]
            for key, value in answers.items()
            if str(value).strip()
        }
    }


def count_progress(progress_payload: dict, solution_payload: dict) -> tuple[int, int]:
    answers = progress_payload.get("answers", {})
    if not isinstance(answers, dict):
        answers = {}
    expected_count = sum(1 for row in solution_payload.get("grid", []) for cell in row if cell)
    found_count = 0
    for key, value in answers.items():
        try:
            x_text, y_text = str(key).split(",", 1)
            x = int(x_text)
            y = int(y_text)
        except ValueError:
            continue
        grid = solution_payload.get("grid", [])
        if 0 <= y < len(grid) and 0 <= x < len(grid[y]) and str(grid[y][x]).upper() == str(value).upper():
            found_count += 1
    return found_count, expected_count


def generate(theme: str, difficulty: str, source_data: list[dict] | None = None) -> GeneratedPuzzle:
    raw_entries = source_data if source_data is not None else [
        {"word": word, "clue": f"Word from {theme.replace('_', ' ')}."}
        for word in words_for_theme(theme, difficulty)
    ]
    count = {"easy": 5, "medium": 7, "hard": 9}.get(difficulty, 5)
    size = {"easy": 11, "medium": 13, "hard": 15}.get(difficulty, 11)
    entries = []
    seen = set()
    for entry in raw_entries:
        word = "".join(ch for ch in str(entry.get("word", "")).upper() if ch.isalnum())
        if len(word) < 3 or word in seen:
            continue
        seen.add(word)
        entries.append(
            {
                "word": word,
                "clue": str(entry.get("clue") or f"{word.title()} entry."),
                "definition_id": entry.get("definition_id"),
            }
        )
        if len(entries) >= count:
            break

    if len(entries) < 3:
        raise ValueError("Crossword generation needs at least three words of 3+ characters.")

    seed = f"crossword:{theme}:{difficulty}:{','.join(item['word'] for item in entries)}"
    rng = random.Random(seed)
    grid: list[list[str | None]] = [[None for _ in range(size)] for _ in range(size)]
    placements: list[dict] = []

    def can_place(word: str, x: int, y: int, direction: str) -> bool:
        dx, dy = (1, 0) if direction == "across" else (0, 1)
        end_x = x + dx * (len(word) - 1)
        end_y = y + dy * (len(word) - 1)
        if x < 0 or y < 0 or end_x >= size or end_y >= size:
            return False
        before_x, before_y = x - dx, y - dy
        after_x, after_y = end_x + dx, end_y + dy
        if 0 <= before_x < size and 0 <= before_y < size and grid[before_y][before_x]:
            return False
        if 0 <= after_x < size and 0 <= after_y < size and grid[after_y][after_x]:
            return False

        touches = 0
        for index, letter in enumerate(word):
            cx, cy = x + dx * index, y + dy * index
            existing = grid[cy][cx]
            if existing is not None and existing != letter:
                return False
            if existing == letter:
                touches += 1
                continue
            side_checks = [(0, -1), (0, 1)] if direction == "across" else [(-1, 0), (1, 0)]
            for sx, sy in side_checks:
                nx, ny = cx + sx, cy + sy
                if 0 <= nx < size and 0 <= ny < size and grid[ny][nx]:
                    return False
        return not placements or touches > 0

    def place(word: str, x: int, y: int, direction: str, clue: str, definition_id: int | None) -> None:
        dx, dy = (1, 0) if direction == "across" else (0, 1)
        for index, letter in enumerate(word):
            grid[y + dy * index][x + dx * index] = letter
        placements.append(
            {
                "word": word,
                "x": x,
                "y": y,
                "direction": direction,
                "clue": clue,
                "definition_id": definition_id,
            }
        )

    first = max(entries, key=lambda item: len(item["word"]))
    place(
        first["word"],
        max(0, (size - len(first["word"])) // 2),
        size // 2,
        "across",
        first["clue"],
        first.get("definition_id"),
    )

    for entry in sorted([item for item in entries if item is not first], key=lambda item: len(item["word"]), reverse=True):
        word = entry["word"]
        candidates = []
        for y, row in enumerate(grid):
            for x, letter in enumerate(row):
                if not letter or letter not in word:
                    continue
                for index, word_letter in enumerate(word):
                    if word_letter != letter:
                        continue
                    candidates.append((x, y - index, "down"))
                    candidates.append((x - index, y, "across"))
        rng.shuffle(candidates)
        for x, y, direction in candidates:
            if can_place(word, x, y, direction):
                place(word, x, y, direction, entry["clue"], entry.get("definition_id"))
                break

    if len(placements) < 3:
        raise ValueError("Unable to place enough crossword words.")

    used_cells = [(x, y) for y, row in enumerate(grid) for x, value in enumerate(row) if value]
    min_x = max(0, min(x for x, _ in used_cells) - 1)
    max_x = min(size - 1, max(x for x, _ in used_cells) + 1)
    min_y = max(0, min(y for _, y in used_cells) - 1)
    max_y = min(size - 1, max(y for _, y in used_cells) + 1)
    cropped = [[grid[y][x] for x in range(min_x, max_x + 1)] for y in range(min_y, max_y + 1)]

    starts: dict[tuple[int, int], int] = {}
    next_number = 1
    for placement in sorted(placements, key=lambda item: (item["y"], item["x"])):
        key = (placement["x"] - min_x, placement["y"] - min_y)
        if key not in starts:
            starts[key] = next_number
            next_number += 1
        placement["number"] = starts[key]
        placement["x"] -= min_x
        placement["y"] -= min_y

    clue_entries = [
        {
            "number": item["number"],
            "direction": item["direction"],
            "clue": item["clue"],
            "answer": item["word"],
            "length": len(item["word"]),
            "x": item["x"],
            "y": item["y"],
            "definition_id": item.get("definition_id"),
        }
        for item in sorted(placements, key=lambda item: (item["direction"], item["number"]))
    ]
    play_cells = []
    print_cells = []
    for y, row in enumerate(cropped):
        play_row = []
        print_row = []
        for x, value in enumerate(row):
            number = starts.get((x, y), "")
            play_row.append({"letter": value or "", "number": number})
            print_row.append({"open": bool(value), "number": number})
        play_cells.append(play_row)
        print_cells.append(print_row)

    solution_grid = [[cell["letter"] for cell in row] for row in play_cells]
    prompt = "Fill the crossword from the clues."
    play_payload = {
        "kind": "crossword",
        "grid": play_cells,
        "clues": clue_entries,
    }
    print_payload = {
        "kind": "crossword",
        "grid": print_cells,
        "clues": [{key: value for key, value in clue.items() if key != "answer"} for clue in clue_entries],
    }
    solution_payload = {
        "entries": clue_entries,
        "grid": solution_grid,
    }
    return GeneratedPuzzle(
        title=f"{theme.replace('_', ' ').title()} Crossword",
        prompt=prompt,
        print_payload=print_payload,
        play_payload=play_payload,
        solution_payload=solution_payload,
        hints=[
            f"There are {len(clue_entries)} crossword answers.",
            "Use intersecting letters to narrow down uncertain answers.",
            f"The longest answer has {max(len(item['answer']) for item in clue_entries)} letters.",
        ],
        metadata={"generator": "local.crossword.v1", "grid_size": len(play_cells)},
    )


register_puzzle_type(
    PuzzleType(
        slug="crossword",
        label="Crossword",
        generator=generate,
        source_builder=build_source,
        answer_checker=check_answer,
        progress_normalizer=normalize_progress,
        progress_counter=count_progress,
    )
)
