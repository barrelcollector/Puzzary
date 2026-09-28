from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GeneratedPuzzle:
    title: str
    prompt: str
    print_payload: dict
    play_payload: dict
    solution_payload: dict
    hints: list[str]
    metadata: dict


@dataclass(frozen=True)
class PuzzleType:
    slug: str
    label: str
    generator: Callable[[str, str, Any, str | None], GeneratedPuzzle]
    source_builder: Callable[[Any, str, set[int] | None], Any]
    answer_checker: Callable[[str, dict], tuple[bool, int, int]]
    progress_normalizer: Callable[[Any, dict], dict]
    progress_counter: Callable[[dict, dict], tuple[int, int]]


PUZZLE_TYPES: dict[str, PuzzleType] = {}


def register_puzzle_type(puzzle_type: PuzzleType) -> PuzzleType:
    if puzzle_type.slug in PUZZLE_TYPES:
        raise ValueError(f"Puzzle type already registered: {puzzle_type.slug}")
    PUZZLE_TYPES[puzzle_type.slug] = puzzle_type
    return puzzle_type


def get_puzzle_type(slug: str) -> PuzzleType | None:
    return PUZZLE_TYPES.get(slug)


def list_puzzle_types() -> list[PuzzleType]:
    return sorted(PUZZLE_TYPES.values(), key=lambda item: item.label)


def normalize_answer(value: str) -> set[str]:
    parts = value.replace("\n", ",").replace(";", ",").split(",")
    return {part.strip().upper() for part in parts if part.strip()}


def load_builtin_puzzle_types() -> None:
    from app.puzzle_types import crossword, word_search  # noqa: F401


load_builtin_puzzle_types()
