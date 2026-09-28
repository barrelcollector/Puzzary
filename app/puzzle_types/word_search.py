from __future__ import annotations

import random
import string
from typing import Any

from app.content import words_for_theme
from app.puzzles import GeneratedPuzzle, PuzzleType, normalize_answer, register_puzzle_type


def normalize_word(value: str) -> str:
    return "".join(ch for ch in str(value).upper() if ch.isalnum())


def word_allowed(word: Any, excluded_tag_ids: set[int] | None) -> bool:
    if getattr(word, "archived_at", None) is not None or getattr(word, "banned_at", None) is not None:
        return False
    if excluded_tag_ids and any(getattr(tag, "id", None) in excluded_tag_ids for tag in getattr(word, "tags", [])):
        return False
    return True


def build_source(word_bank: Any, difficulty: str, excluded_tag_ids: set[int] | None = None) -> list[str] | None:
    if word_bank is None:
        return None
    words = [
        link.word.word
        for link in sorted(word_bank.word_links, key=lambda item: item.position)
        if word_allowed(link.word, excluded_tag_ids)
    ]
    if not words:
        payload_words = word_bank.payload.get("words", [])
        words = [str(word).upper() for word in payload_words if str(word).strip()] if isinstance(payload_words, list) else []
    if not words:
        return None
    count = {"easy": 5, "medium": 7, "hard": 8}.get(difficulty, 5)
    return words[:count]


def check_answer(answer: str, solution_payload: dict) -> tuple[bool, int, int]:
    expected = set(solution_payload["words"])
    supplied = normalize_answer(answer)
    matched = len(expected & supplied)
    return expected <= supplied, len(expected), matched


def normalize_progress(payload: Any, solution_payload: dict) -> dict:
    expected = {str(word).upper() for word in solution_payload.get("words", [])}
    raw_words = getattr(payload, "found_words", []) if payload is not None else []
    supplied = {str(word).strip().upper() for word in raw_words if str(word).strip()}
    return {"found_words": sorted(expected & supplied)}


def count_progress(progress_payload: dict, solution_payload: dict) -> tuple[int, int]:
    expected_count = len(solution_payload.get("words", []))
    words = progress_payload.get("found_words", [])
    if not isinstance(words, list):
        words = []
    return len(words), expected_count


def contains_blocked_word(grid: list[list[str]], blocked_words: set[str]) -> bool:
    if not blocked_words:
        return False
    size = len(grid)
    directions = [(1, 0), (0, 1), (1, 1), (-1, 1)]
    shortest = min(len(word) for word in blocked_words)
    for y in range(size):
        for x in range(size):
            for dx, dy in directions:
                px, py = x, y
                line = []
                while 0 <= px < size and 0 <= py < size:
                    line.append(grid[py][px])
                    px += dx
                    py += dy
                if len(line) < shortest:
                    continue
                text = "".join(line)
                reversed_text = text[::-1]
                if any(word in text or word in reversed_text for word in blocked_words):
                    return True
    return False


def fill_grid(base_grid: list[list[str]], rng: random.Random, blocked_words: set[str]) -> list[list[str]]:
    for _ in range(250):
        grid = [row[:] for row in base_grid]
        for y, row in enumerate(grid):
            for x, value in enumerate(row):
                if not value:
                    grid[y][x] = rng.choice(string.ascii_uppercase)
        if not contains_blocked_word(grid, blocked_words):
            return grid
    raise ValueError("Unable to fill word search without forming a blocked word.")


def generate(theme: str, difficulty: str, source_data: list[str] | dict | None = None, seed: str | None = None) -> GeneratedPuzzle:
    blocked_source = []
    if isinstance(source_data, dict):
        raw_words = source_data.get("words")
        source_words = raw_words if raw_words is not None else words_for_theme(theme, difficulty)
        blocked_source = source_data.get("blocked_words", [])
    else:
        source_words = source_data if source_data is not None else words_for_theme(theme, difficulty)
    blocked_words = {normalize_word(word) for word in blocked_source if len(normalize_word(word)) >= 3}
    words = [normalize_word(word) for word in source_words]
    words = [
        word
        for word in words
        if word and not any(blocked in word or blocked in word[::-1] for blocked in blocked_words)
    ]
    if not words:
        raise ValueError("Word search generation needs at least one allowed word.")
    size = {"easy": 10, "medium": 12, "hard": 14}.get(difficulty, 10)
    generation_seed = seed or f"{theme}:{difficulty}:{','.join(words)}"
    rng = random.Random(generation_seed)
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

    grid = fill_grid(grid, rng, blocked_words)

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
        metadata={
            "generator": "local.word_search.v1",
            "grid_size": size,
            "blocked_word_count": len(blocked_words),
            "seed": generation_seed,
        },
    )


register_puzzle_type(
    PuzzleType(
        slug="word_search",
        label="Word search",
        generator=generate,
        source_builder=build_source,
        answer_checker=check_answer,
        progress_normalizer=normalize_progress,
        progress_counter=count_progress,
    )
)
