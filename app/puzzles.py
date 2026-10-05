from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class GenerationField:
    name: str
    label: str
    field_type: str = "text"
    default: Any = ""
    required: bool = False
    help_text: str = ""
    options: tuple[dict[str, str], ...] = ()
    minimum: int | None = None
    maximum: int | None = None


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
class PrintLayout:
    slug: str = "default"
    sheet_class: str = "print-layout-default"
    page_size: str = "receipt"
    fit_to_width: bool = True
    include_open_link: bool = True
    include_solution: bool = False
    show_prompt: bool = True
    instructions: str = ""
    notes: tuple[str, ...] = ()
    max_width_px: int = 760

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["notes"] = list(self.notes)
        return data


@dataclass(frozen=True)
class PuzzleType:
    slug: str
    label: str
    generator: Callable[[str, str, Any, str | None, dict[str, Any] | None], GeneratedPuzzle]
    source_builder: Callable[[Any, str, set[int] | None], Any]
    answer_checker: Callable[[str, dict], tuple[bool, int, int]]
    progress_normalizer: Callable[[Any, dict], dict]
    progress_counter: Callable[[dict, dict], tuple[int, int]]
    description: str = ""
    supports_print: bool = True
    supports_web_play: bool = True
    digital_only: bool = False
    source_requirements: tuple[str, ...] = ()
    print_layout: PrintLayout = field(default_factory=PrintLayout)
    print_layout_builder: Callable[[dict, dict, dict], dict] | None = None
    supports_gimmes: bool = False
    gimme_modes: tuple[str, ...] = ()
    generation_fields: tuple[GenerationField, ...] = ()
    manual_editor_fields: tuple[GenerationField, ...] = ()
    manual_builder: Callable[[dict[str, Any]], GeneratedPuzzle] | None = None
    theme_mode: str = "free_text"
    preview_builder: Callable[[dict, dict, dict], dict] | None = None


def fallback_preview(kind: str, label: str) -> dict:
    return {
        "kind": "fallback",
        "label": label,
        "icon": "".join(part[:1] for part in label.split()[:2]).upper() or "?",
        "meta": kind.replace("_", " "),
    }


PUZZLE_TYPES: dict[str, PuzzleType] = {}
_PUZZLE_TYPES_LOADED = False


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


def load_puzzle_types(package_name: str = "app.puzzle_types") -> None:
    global _PUZZLE_TYPES_LOADED
    if _PUZZLE_TYPES_LOADED:
        return

    package = importlib.import_module(package_name)
    for module_info in pkgutil.iter_modules(package.__path__, f"{package_name}."):
        module_name = module_info.name.rsplit(".", 1)[-1]
        if module_name.startswith("_"):
            continue
        importlib.import_module(module_info.name)
    _PUZZLE_TYPES_LOADED = True


load_puzzle_types()
