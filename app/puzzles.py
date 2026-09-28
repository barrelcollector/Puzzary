from __future__ import annotations

import random
import string
from dataclasses import dataclass

from app.content import words_for_theme


@dataclass(frozen=True)
class GeneratedPuzzle:
    title: str
    prompt: str
    print_payload: dict
    play_payload: dict
    solution_payload: dict
    hints: list[str]
    metadata: dict


def normalize_answer(value: str) -> set[str]:
    parts = value.replace("\n", ",").replace(";", ",").split(",")
    return {part.strip().upper() for part in parts if part.strip()}


def check_word_search_answer(answer: str, solution_payload: dict) -> tuple[bool, int, int]:
    expected = set(solution_payload["words"])
    supplied = normalize_answer(answer)
    matched = len(expected & supplied)
    return expected <= supplied, len(expected), matched


def check_crossword_answer(answer: str, solution_payload: dict) -> tuple[bool, int, int]:
    expected = {str(entry["answer"]).upper() for entry in solution_payload.get("entries", [])}
    supplied = normalize_answer(answer)
    matched = len(expected & supplied)
    return expected <= supplied, len(expected), matched


def generate_word_search(theme: str, difficulty: str, words_override: list[str] | None = None) -> GeneratedPuzzle:
    source_words = words_override if words_override is not None else words_for_theme(theme, difficulty)
    words = [word.upper() for word in source_words]
    size = {"easy": 10, "medium": 12, "hard": 14}.get(difficulty, 10)
    seed = f"{theme}:{difficulty}:{','.join(words)}"
    rng = random.Random(seed)
    grid = [["" for _ in range(size)] for _ in range(size)]
    placements: list[dict] = []
    directions = [(1, 0), (0, 1), (1, 1), (-1, 1)]

    for word in sorted(words, key=len, reverse=True):
        placed = False
        for _ in range(200):
            dx, dy = rng.choice(directions)
            min_x = 0 if dx >= 0 else len(word) - 1
            max_x = size - len(word) if dx >= 0 else size - 1
            min_y = 0
            max_y = size - len(word) if dy > 0 else size - 1
            x = rng.randint(min_x, max_x)
            y = rng.randint(min_y, max_y)
            coords = [(x + i * dx, y + i * dy) for i in range(len(word))]
            if all(grid[cy][cx] in {"", letter} for (cx, cy), letter in zip(coords, word)):
                for (cx, cy), letter in zip(coords, word):
                    grid[cy][cx] = letter
                placements.append({"word": word, "start": [x, y], "direction": [dx, dy]})
                placed = True
                break
        if not placed:
            raise ValueError(f"Unable to place word: {word}")

    for y in range(size):
        for x in range(size):
            if not grid[y][x]:
                grid[y][x] = rng.choice(string.ascii_uppercase)

    prompt = "Find all listed words."
    print_payload = {
        "kind": "word_search",
        "grid": ["".join(row) for row in grid],
        "words": words,
    }
    solution_payload = {
        "words": words,
        "placements": placements,
    }
    return GeneratedPuzzle(
        title=f"{theme.replace('_', ' ').title()} Word Search",
        prompt=prompt,
        print_payload=print_payload,
        play_payload=print_payload,
        solution_payload=solution_payload,
        hints=[
            f"There are {len(words)} words hidden in the grid.",
            "Words may go across, down, or diagonally.",
            f"The words are: {', '.join(words)}.",
        ],
        metadata={"generator": "local.word_search.v1", "grid_size": size},
    )


def generate_crossword(theme: str, difficulty: str, entries_override: list[dict] | None = None) -> GeneratedPuzzle:
    raw_entries = entries_override if entries_override is not None else [
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
        entries.append({"word": word, "clue": str(entry.get("clue") or f"{word.title()} entry.")})
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

    def place(word: str, x: int, y: int, direction: str, clue: str) -> None:
        dx, dy = (1, 0) if direction == "across" else (0, 1)
        for index, letter in enumerate(word):
            grid[y + dy * index][x + dx * index] = letter
        placements.append({"word": word, "x": x, "y": y, "direction": direction, "clue": clue})

    first = max(entries, key=lambda item: len(item["word"]))
    place(first["word"], max(0, (size - len(first["word"])) // 2), size // 2, "across", first["clue"])

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
                place(word, x, y, direction, entry["clue"])
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
