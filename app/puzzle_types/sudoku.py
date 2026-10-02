from __future__ import annotations

import json
import random
from typing import Any

from app.puzzles import GeneratedPuzzle, PuzzleType, register_puzzle_type


Grid = list[list[int]]


def build_source(_source_entries: Any, _difficulty: str, _excluded_tag_ids: set[int] | None = None) -> dict:
    return {}


def base_solution() -> Grid:
    return [[((row * 3 + row // 3 + col) % 9) + 1 for col in range(9)] for row in range(9)]


def shuffled_solution(rng: random.Random) -> Grid:
    rows = [band * 3 + row for band in rng.sample(range(3), 3) for row in rng.sample(range(3), 3)]
    cols = [stack * 3 + col for stack in rng.sample(range(3), 3) for col in rng.sample(range(3), 3)]
    values = rng.sample(range(1, 10), 9)
    base = base_solution()
    return [[values[base[row][col] - 1] for col in cols] for row in rows]


def puzzle_from_solution(solution: Grid, difficulty: str, rng: random.Random) -> Grid:
    givens = {"easy": 42, "medium": 34, "hard": 28}.get(difficulty, 34)
    puzzle = [row[:] for row in solution]
    cells = [(row, col) for row in range(9) for col in range(9)]
    rng.shuffle(cells)
    for row, col in cells[: 81 - givens]:
        puzzle[row][col] = 0
    return puzzle


def flat_answer_from_payload(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        if isinstance(parsed, dict):
            value = parsed
        else:
            digits = [char for char in str(parsed) if char.isdigit()]
            return {f"{index % 9},{index // 9}": digit for index, digit in enumerate(digits[:81]) if digit != "0"}
    if not isinstance(value, dict):
        return {}
    answers = value.get("answers", value)
    if not isinstance(answers, dict):
        return {}
    normalized = {}
    for key, raw_value in answers.items():
        digit = str(raw_value).strip()[:1]
        if key and digit in "123456789":
            normalized[str(key)] = digit
    return normalized


def given_cells(puzzle_grid: Grid) -> dict[str, str]:
    return {
        f"{col},{row}": str(value)
        for row, line in enumerate(puzzle_grid)
        for col, value in enumerate(line)
        if value
    }


def check_answer(answer: str, solution_payload: dict) -> tuple[bool, int, int]:
    supplied = flat_answer_from_payload(answer)
    expected = {
        f"{col},{row}": str(value)
        for row, line in enumerate(solution_payload.get("grid", []))
        for col, value in enumerate(line)
    }
    givens = {str(key): str(value) for key, value in solution_payload.get("givens", {}).items()}
    supplied = {**givens, **supplied}
    matched = sum(1 for key, value in expected.items() if supplied.get(key) == value)
    return matched == len(expected), len(expected), matched


def normalize_progress(payload: Any, solution_payload: dict) -> dict:
    supplied = flat_answer_from_payload(getattr(payload, "answers", {}) if payload is not None else {})
    givens = {str(key): str(value) for key, value in solution_payload.get("givens", {}).items()}
    return {"answers": {key: value for key, value in supplied.items() if key not in givens}}


def count_progress(progress_payload: dict, solution_payload: dict) -> tuple[int, int]:
    expected = {
        f"{col},{row}": str(value)
        for row, line in enumerate(solution_payload.get("grid", []))
        for col, value in enumerate(line)
    }
    givens = {str(key): str(value) for key, value in solution_payload.get("givens", {}).items()}
    supplied = progress_payload.get("answers", {}) if isinstance(progress_payload, dict) else {}
    if not isinstance(supplied, dict):
        supplied = {}
    combined = {**givens, **{str(key): str(value) for key, value in supplied.items()}}
    matched = sum(1 for key, value in expected.items() if combined.get(key) == value)
    return matched, len(expected)


def generate(theme: str, difficulty: str, _source_data: Any = None, seed: str | None = None) -> GeneratedPuzzle:
    generation_seed = seed or f"sudoku:{theme}:{difficulty}"
    rng = random.Random(generation_seed)
    solution = shuffled_solution(rng)
    puzzle = puzzle_from_solution(solution, difficulty, rng)
    givens = given_cells(puzzle)
    blank_count = 81 - len(givens)
    payload = {
        "kind": "sudoku",
        "grid": puzzle,
        "givens": givens,
    }
    solution_payload = {
        "grid": solution,
        "givens": givens,
    }
    return GeneratedPuzzle(
        title=f"{theme.replace('_', ' ').title()} Sudoku",
        prompt="Fill every row, column, and 3x3 box with digits 1 through 9.",
        print_payload=payload,
        play_payload=payload,
        solution_payload=solution_payload,
        hints=[
            "Each row must contain 1 through 9 without repeats.",
            "Each column must contain 1 through 9 without repeats.",
            "Each 3x3 box must contain 1 through 9 without repeats.",
            f"This puzzle starts with {len(givens)} givens and {blank_count} blanks.",
        ],
        metadata={
            "generator": "local.sudoku.v1",
            "seed": generation_seed,
            "givens": len(givens),
            "blanks": blank_count,
        },
    )


register_puzzle_type(
    PuzzleType(
        slug="sudoku",
        label="Sudoku",
        generator=generate,
        source_builder=build_source,
        answer_checker=check_answer,
        progress_normalizer=normalize_progress,
        progress_counter=count_progress,
        description="A classic 9x9 number-placement puzzle.",
        supports_print=True,
        supports_web_play=True,
    )
)
