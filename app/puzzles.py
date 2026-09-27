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


def generate_word_search(theme: str, difficulty: str) -> GeneratedPuzzle:
    words = [word.upper() for word in words_for_theme(theme, difficulty)]
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
