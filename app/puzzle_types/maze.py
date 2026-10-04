from __future__ import annotations

import json
import random
from typing import Any

from app.puzzles import GeneratedPuzzle, GenerationField, PrintLayout, PuzzleType, register_puzzle_type


DIRECTIONS = {
    "n": (0, -1, "s"),
    "e": (1, 0, "w"),
    "s": (0, 1, "n"),
    "w": (-1, 0, "e"),
}


def build_source(_source_entries: Any, _difficulty: str, _excluded_tag_ids: set[int] | None = None) -> dict:
    return {}


def integer_option(options: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(options.get(key, "")).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def bool_option(options: dict[str, Any], key: str, default: bool) -> bool:
    raw = str(options.get(key, "")).strip().lower()
    if raw in {"yes", "true", "1", "on"}:
        return True
    if raw in {"no", "false", "0", "off"}:
        return False
    return default


def maze_size(options: dict[str, Any], difficulty: str) -> tuple[int, int, str]:
    preset = str(options.get("maze_size", "")).strip().lower()
    if preset == "small":
        return 10, 10, "small"
    if preset == "medium":
        return 14, 14, "medium"
    if preset == "large":
        return 18, 18, "large"
    if preset == "wide":
        return 20, 12, "wide"
    if preset == "custom":
        width = integer_option(options, "custom_width", 14, 5, 30)
        height = integer_option(options, "custom_height", width, 5, 30)
        return width, height, "custom"
    defaults = {
        "easy": (10, 10, "small"),
        "medium": (14, 14, "medium"),
        "hard": (18, 18, "large"),
    }
    return defaults.get(difficulty, defaults["medium"])


def new_grid(width: int, height: int) -> list[list[dict[str, Any]]]:
    return [
        [
            {"x": x, "y": y, "walls": {"n": True, "e": True, "s": True, "w": True}}
            for x in range(width)
        ]
        for y in range(height)
    ]


def carve_maze(width: int, height: int, rng: random.Random) -> list[list[dict[str, Any]]]:
    grid = new_grid(width, height)
    visited = {(0, 0)}
    stack = [(0, 0)]
    while stack:
        x, y = stack[-1]
        neighbors = []
        for direction, (dx, dy, opposite) in DIRECTIONS.items():
            nx, ny = x + dx, y + dy
            if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in visited:
                neighbors.append((direction, opposite, nx, ny))
        if not neighbors:
            stack.pop()
            continue
        direction, opposite, nx, ny = rng.choice(neighbors)
        grid[y][x]["walls"][direction] = False
        grid[ny][nx]["walls"][opposite] = False
        visited.add((nx, ny))
        stack.append((nx, ny))
    grid[0][0]["walls"]["w"] = False
    grid[height - 1][width - 1]["walls"]["e"] = False
    return grid


def add_loops(grid: list[list[dict[str, Any]]], rng: random.Random, loop_count: int) -> None:
    height = len(grid)
    width = len(grid[0]) if height else 0
    candidates = []
    for y in range(height):
        for x in range(width):
            for direction, (dx, dy, opposite) in DIRECTIONS.items():
                nx, ny = x + dx, y + dy
                if direction not in {"e", "s"}:
                    continue
                if 0 <= nx < width and 0 <= ny < height and grid[y][x]["walls"][direction]:
                    candidates.append((x, y, direction, nx, ny, opposite))
    rng.shuffle(candidates)
    for x, y, direction, nx, ny, opposite in candidates[:loop_count]:
        grid[y][x]["walls"][direction] = False
        grid[ny][nx]["walls"][opposite] = False


def solve_maze(grid: list[list[dict[str, Any]]]) -> list[dict[str, int]]:
    height = len(grid)
    width = len(grid[0]) if height else 0
    target = (width - 1, height - 1)
    queue = [(0, 0)]
    parents: dict[tuple[int, int], tuple[int, int] | None] = {(0, 0): None}
    for x, y in queue:
        if (x, y) == target:
            break
        for direction, (dx, dy, _opposite) in DIRECTIONS.items():
            if grid[y][x]["walls"][direction]:
                continue
            nx, ny = x + dx, y + dy
            if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in parents:
                parents[(nx, ny)] = (x, y)
                queue.append((nx, ny))
    if target not in parents:
        raise ValueError("Maze generation produced an unsolvable layout.")
    path = []
    cursor: tuple[int, int] | None = target
    while cursor is not None:
        x, y = cursor
        path.append({"x": x, "y": y})
        cursor = parents[cursor]
    path.reverse()
    return path


def solution_polyline(path: list[dict[str, int]], cell_size: int) -> str:
    half = cell_size // 2
    return " ".join(f"{point['x'] * cell_size + half},{point['y'] * cell_size + half}" for point in path)


def parse_answer(answer: Any) -> dict[str, Any]:
    if isinstance(answer, str):
        if answer.strip().lower() in {"done", "complete", "completed", "solved", "true", "yes"}:
            return {"completed": True}
        try:
            parsed = json.loads(answer)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed.get("answers", parsed)
    if isinstance(answer, dict):
        return answer.get("answers", answer)
    return {}


def check_answer(answer: str, _solution_payload: dict) -> tuple[bool, int, int]:
    payload = parse_answer(answer)
    completed = str(payload.get("completed", "")).lower() in {"true", "yes", "1", "done", "complete", "solved"}
    return completed, 1, 1 if completed else 0


def normalize_progress(payload: Any, _solution_payload: dict) -> dict:
    raw_answers = getattr(payload, "answers", {}) if payload is not None else {}
    completed = str(raw_answers.get("completed", "")).lower() in {"true", "yes", "1", "done", "complete", "solved"}
    return {"answers": {"completed": "true" if completed else ""}}


def count_progress(progress_payload: dict, _solution_payload: dict) -> tuple[int, int]:
    answers = progress_payload.get("answers", {}) if isinstance(progress_payload, dict) else {}
    completed = str(answers.get("completed", "")).lower() in {"true", "yes", "1", "done", "complete", "solved"}
    return (1 if completed else 0), 1


def build_preview(print_payload: dict, play_payload: dict, _metadata: dict) -> dict:
    payload = print_payload if isinstance(print_payload, dict) else play_payload
    settings = payload.get("settings", {}) if isinstance(payload, dict) else {}
    return {
        "kind": "maze",
        "width": int(settings.get("width", 10)),
        "height": int(settings.get("height", 10)),
        "meta": f"{settings.get('width', 10)}x{settings.get('height', 10)} maze",
    }


def generate(
    theme: str,
    difficulty: str,
    _source_data: Any = None,
    seed: str | None = None,
    _generation_options: dict[str, Any] | None = None,
) -> GeneratedPuzzle:
    generation_options = _generation_options or {}
    width, height, size_label = maze_size(generation_options, difficulty)
    generation_seed = seed or f"maze:{difficulty}:{width}x{height}"
    rng = random.Random(generation_seed)
    grid = carve_maze(width, height, rng)
    loop_default = {"easy": 2, "medium": 1, "hard": 0}.get(difficulty, 1)
    loop_count = integer_option(generation_options, "loop_count", loop_default, 0, max(0, (width * height) // 6))
    if bool_option(generation_options, "allow_loops", loop_count > 0):
        add_loops(grid, rng, loop_count)
    path = solve_maze(grid)
    cell_size = 28 if max(width, height) <= 14 else 22 if max(width, height) <= 22 else 18
    settings = {
        "width": width,
        "height": height,
        "size": size_label,
        "cell_size": cell_size,
        "allow_loops": loop_count > 0,
        "loop_count": loop_count,
    }
    payload = {
        "kind": "maze",
        "grid": grid,
        "settings": settings,
        "start": {"x": 0, "y": 0},
        "end": {"x": width - 1, "y": height - 1},
    }
    solution_payload = payload | {
        "path": path,
        "path_length": len(path),
        "solution_polyline": solution_polyline(path, cell_size),
    }
    title_prefix = theme.replace("_", " ").replace("-", " ").title().strip()
    title = f"{title_prefix} Maze" if title_prefix else "Maze"
    return GeneratedPuzzle(
        title=title,
        prompt="Draw a path from Start to Finish without crossing any walls.",
        print_payload=payload,
        play_payload=payload,
        solution_payload=solution_payload,
        hints=[
            "Start at the opening on the left.",
            "Finish at the opening on the right.",
            f"The solution path uses {len(path)} cells.",
        ],
        metadata={
            "generator": "local.maze.v1",
            "seed": generation_seed,
            "path_length": len(path),
            "generation_options": settings,
        },
    )


register_puzzle_type(
    PuzzleType(
        slug="maze",
        label="Maze",
        generator=generate,
        source_builder=build_source,
        answer_checker=check_answer,
        progress_normalizer=normalize_progress,
        progress_counter=count_progress,
        preview_builder=build_preview,
        description="A printable maze with a start, finish, and admin-visible solution path.",
        supports_print=True,
        supports_web_play=True,
        theme_mode="none",
        print_layout=PrintLayout(
            slug="maze",
            sheet_class="print-layout-maze",
            instructions="Draw a path from Start to Finish without crossing any walls.",
            notes=("Admin solution view overlays the solved path.",),
            max_width_px=760,
        ),
        generation_fields=(
            GenerationField(
                name="maze_size",
                label="Maze size",
                field_type="select",
                default="",
                options=(
                    {"value": "", "label": "Difficulty default"},
                    {"value": "small", "label": "Small 10x10"},
                    {"value": "medium", "label": "Medium 14x14"},
                    {"value": "large", "label": "Large 18x18"},
                    {"value": "wide", "label": "Wide 20x12"},
                    {"value": "custom", "label": "Custom"},
                ),
            ),
            GenerationField(
                name="custom_width",
                label="Custom width",
                field_type="number",
                default="",
                help_text="Only used for Custom.",
                minimum=5,
                maximum=30,
            ),
            GenerationField(
                name="custom_height",
                label="Custom height",
                field_type="number",
                default="",
                help_text="Only used for Custom.",
                minimum=5,
                maximum=30,
            ),
            GenerationField(
                name="allow_loops",
                label="Allow loops",
                field_type="select",
                default="difficulty",
                options=(
                    {"value": "difficulty", "label": "Difficulty default"},
                    {"value": "yes", "label": "Yes"},
                    {"value": "no", "label": "No"},
                ),
            ),
            GenerationField(
                name="loop_count",
                label="Loop count",
                field_type="number",
                default="",
                help_text="Optional. Extra openings make alternate paths.",
                minimum=0,
                maximum=150,
            ),
        ),
    )
)
