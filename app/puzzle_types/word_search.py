from __future__ import annotations

import random
import re
import string
from typing import Any

from app.content import words_for_theme
from app.puzzles import GeneratedPuzzle, GenerationField, PrintLayout, PuzzleType, normalize_answer, register_puzzle_type


DIRECTION_PRESETS: dict[str, list[tuple[int, int]]] = {
    "horizontal_vertical": [(1, 0), (0, 1)],
    "horizontal_vertical_diagonal": [(1, 0), (0, 1), (1, 1), (-1, 1)],
    "all_forward": [(1, 0), (0, 1), (1, 1), (-1, 1)],
    "all": [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)],
}


def normalize_word(value: str) -> str:
    return "".join(ch for ch in str(value).upper() if ch.isalnum())


def word_allowed(word: Any, excluded_tag_ids: set[int] | None) -> bool:
    if getattr(word, "archived_at", None) is not None or getattr(word, "banned_at", None) is not None:
        return False
    if excluded_tag_ids and any(getattr(tag, "id", None) in excluded_tag_ids for tag in getattr(word, "tags", [])):
        return False
    return True


def build_source(word_entries: Any, difficulty: str, excluded_tag_ids: set[int] | None = None) -> list[str] | None:
    words = [
        entry["word"].word
        for entry in word_entries or []
        if isinstance(entry, dict) and entry.get("word") is not None and word_allowed(entry["word"], excluded_tag_ids)
    ]
    if not words:
        return None
    return words


def gimme_words_for_difficulty(words: list[str], difficulty: str) -> list[str]:
    count = {"easy": 1, "medium": 1, "hard": 0}.get(difficulty, 0)
    return words[: min(count, max(0, len(words) - 1))]


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


def grid_size_option(options: dict[str, Any], difficulty: str) -> int:
    default = {"easy": 10, "medium": 12, "hard": 14}.get(difficulty, 10)
    preset = str(options.get("grid_size", "")).strip().lower()
    if preset == "custom":
        return integer_option(options, "custom_grid_size", default, 6, 24)
    if preset:
        try:
            return max(6, min(24, int(preset)))
        except ValueError:
            pass
    return default


def direction_options(options: dict[str, Any]) -> tuple[str, list[tuple[int, int]]]:
    preset = str(options.get("directions", "horizontal_vertical_diagonal")).strip().lower()
    if preset not in DIRECTION_PRESETS:
        preset = "horizontal_vertical_diagonal"
    return preset, DIRECTION_PRESETS[preset]


def coordinate_bounds(length: int, delta: int, size: int) -> tuple[int, int]:
    if delta > 0:
        return 0, size - length
    if delta < 0:
        return length - 1, size - 1
    return 0, size - 1


def check_answer(answer: str, solution_payload: dict) -> tuple[bool, int, int]:
    expected = set(solution_payload["words"])
    supplied = normalize_answer(answer) | {str(word).upper() for word in solution_payload.get("gimmes", {}).get("words", [])}
    matched = len(expected & supplied)
    return expected <= supplied, len(expected), matched


def normalize_progress(payload: Any, solution_payload: dict) -> dict:
    expected = {str(word).upper() for word in solution_payload.get("words", [])}
    raw_words = getattr(payload, "found_words", []) if payload is not None else []
    supplied = {str(word).strip().upper() for word in raw_words if str(word).strip()}
    supplied |= {str(word).upper() for word in solution_payload.get("gimmes", {}).get("words", [])}
    return {"found_words": sorted(expected & supplied)}


def count_progress(progress_payload: dict, solution_payload: dict) -> tuple[int, int]:
    expected_count = len(solution_payload.get("words", []))
    words = progress_payload.get("found_words", [])
    if not isinstance(words, list):
        words = []
    found = {str(word).upper() for word in words}
    found |= {str(word).upper() for word in solution_payload.get("gimmes", {}).get("words", [])}
    expected = {str(word).upper() for word in solution_payload.get("words", [])}
    return len(expected & found), expected_count


def collaboration(progress, action, solution):
    from copy import deepcopy
    import re
    result = deepcopy(progress)
    answers = dict(result.get('answers', {}))
    size = int(solution.get('settings', {}).get('grid_size', 10))
    width = int(solution.get('settings', {}).get('grid_width', size))
    height = int(solution.get('settings', {}).get('grid_height', size))

    def coordinates(key):
        match = re.fullmatch(r'guess:(\d+),(\d+):(\d+),(\d+)', key)
        if not match: raise ValueError('Invalid selection')
        x, y, ex, ey = map(int, match.groups())
        if not (0 <= x < width and 0 <= ex < width and 0 <= y < height and 0 <= ey < height):
            raise ValueError('Selection outside the grid')
        dx, dy = ex - x, ey - y
        if dx and dy and abs(dx) != abs(dy): return {(x, y), (ex, ey)}
        count = max(abs(dx), abs(dy)) + 1
        return {(x + i * ((dx > 0) - (dx < 0)), y + i * ((dy > 0) - (dy < 0))) for i in range(count)}

    kind = action['kind']
    if kind == 'patch':
        for key, value in action.get('changes', {}).items():
            coordinates(key)
            if value not in ('', 'pending'): raise ValueError('Invalid selection state')
            if value: answers[key] = value
            else: answers.pop(key, None)
    elif kind not in ('check', 'hint', 'reveal'):
        raise ValueError('Unsupported action')
    found = set(result.get('found_words', [])) | set(solution.get('gimmes', {}).get('words', []))
    confirmed = set(result.get('confirmed_selections', []))
    pending = {k: coordinates(k) for k, v in answers.items() if v == 'pending'}
    matched = set()
    auto = solution.get('settings', {}).get('auto_confirm', True)
    if kind == 'check' or (kind == 'patch' and auto):
        for placement in solution['placements']:
            word = placement['word']
            x, y = placement['start']; dx, dy = placement['direction']
            cells = {(x + i * dx, y + i * dy) for i in range(len(word))}
            candidates = {k: coords for k, coords in pending.items() if coords & cells}
            complete = any(coords == cells for coords in candidates.values())
            if not auto and candidates:
                complete = complete or set().union(*candidates.values()) == cells
            if complete:
                found.add(word)
                matched.update(k for k, coords in candidates.items() if coords <= cells)
        for key in pending:
            if key in matched:
                answers.pop(key, None)
                confirmed.add(key)
            elif kind == 'check' or auto: answers[key] = 'wrong'
    if kind == 'reveal':
        found.update(solution['words'])
        for placement in solution['placements']:
            x, y = placement['start']; dx, dy = placement['direction']; length = len(placement['word'])
            first, last = f'{x},{y}', f'{x + dx * (length - 1)},{y + dy * (length - 1)}'
            ends = sorted((first, last))
            confirmed.add(f'guess:{ends[0]}:{ends[1]}')
    if kind == 'hint':
        word = next((w for w in solution['words'] if w not in found), None)
        if word: found.add(word)
    result['answers'] = answers
    result['found_words'] = sorted(found)
    result['confirmed_selections'] = sorted(confirmed)
    return result


def build_preview(print_payload: dict, play_payload: dict, metadata: dict) -> dict:
    payload = print_payload if isinstance(print_payload, dict) else play_payload
    grid = payload.get("grid", []) if isinstance(payload, dict) else []
    words = payload.get("words", []) if isinstance(payload, dict) else []
    return {
        "kind": "word_search",
        "grid": [str(row)[:10] for row in grid[:10]],
        "words": [str(word).upper() for word in words[:5]],
        "meta": f"{len(words)} words",
    }


def parse_manual_grid(value: Any) -> list[str]:
    rows = []
    for line in str(value or "").splitlines():
        row = "".join(ch for ch in line.upper() if ch.isalnum())
        if row:
            rows.append(row)
    if not rows:
        raise ValueError("Manual word search needs a letter grid.")
    width = len(rows[0])
    if width < 2 or len(rows) < 2:
        raise ValueError("Manual word search grid must be at least 2x2.")
    if any(len(row) != width for row in rows):
        raise ValueError("Every manual word search grid row must have the same length.")
    if len(rows) != width:
        raise ValueError("Manual word search grids must be square for the current web and print renderers.")
    if width > 30 or len(rows) > 30:
        raise ValueError("Manual word search grid can be at most 30x30.")
    return rows


def parse_manual_words(value: Any) -> list[str]:
    raw_parts = re.split(r"[\n,;]+", str(value or ""))
    words = list(dict.fromkeys(normalize_word(part) for part in raw_parts if normalize_word(part)))
    if not words:
        raise ValueError("Manual word search needs at least one word.")
    return words


def find_word_placement(grid: list[str], word: str) -> dict | None:
    height = len(grid)
    width = len(grid[0]) if grid else 0
    directions = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)]
    for y in range(height):
        for x in range(width):
            for dx, dy in directions:
                end_x = x + dx * (len(word) - 1)
                end_y = y + dy * (len(word) - 1)
                if not (0 <= end_x < width and 0 <= end_y < height):
                    continue
                letters = "".join(grid[y + dy * index][x + dx * index] for index in range(len(word)))
                if letters == word:
                    return {"word": word, "start": [x, y], "direction": [dx, dy]}
    return None


def gimme_cells_for_placements(placements: list[dict], gimme_words: list[str]) -> dict[str, str]:
    wanted = {str(word).upper() for word in gimme_words}
    cells = {}
    for placement in placements:
        word = str(placement.get("word", "")).upper()
        if word not in wanted:
            continue
        start = placement.get("start", [])
        direction = placement.get("direction", [])
        if len(start) != 2 or len(direction) != 2:
            continue
        x, y = int(start[0]), int(start[1])
        dx, dy = int(direction[0]), int(direction[1])
        for index, letter in enumerate(word):
            cells[f"{x + dx * index},{y + dy * index}"] = letter
    return cells


def build_manual_puzzle(options: dict[str, Any]) -> GeneratedPuzzle:
    grid = parse_manual_grid(options.get("grid", ""))
    words = parse_manual_words(options.get("words", ""))
    placements = []
    missing = []
    for word in words:
        placement = find_word_placement(grid, word)
        if placement is None:
            missing.append(word)
        else:
            placements.append(placement)
    if missing:
        raise ValueError(f"These words were not found in the grid: {', '.join(missing)}")
    auto_confirm = bool_option(options, "auto_confirm", True)
    print_style = str(options.get("print_style", "boxed")).strip().lower()
    if print_style not in {"boxed", "compact"}:
        print_style = "boxed"
    gimme_count = integer_option(options, "gimme_count", 0, 0, max(0, len(words) - 1))
    gimme_words = words[:gimme_count]
    gimme_cells = gimme_cells_for_placements(placements, gimme_words)
    settings = {
        "grid_size": max(len(grid), len(grid[0])),
        "grid_width": len(grid[0]),
        "grid_height": len(grid),
        "auto_confirm": auto_confirm,
        "word_count": len(words),
        "word_count_min": len(words),
        "word_count_limit": len(words),
        "word_count_max": len(words),
        "print_style": print_style,
        "manual_grid": True,
    }
    prompt = "Find all listed words."
    print_payload = {
        "kind": "word_search",
        "grid": grid,
        "words": words,
        "gimmes": {"mode": "found_words", "words": gimme_words, "cells": gimme_cells},
        "settings": settings,
    }
    solution_payload = {
        "words": words,
        "placements": placements,
        "gimmes": {"mode": "found_words", "words": gimme_words},
        "settings": settings,
    }
    return GeneratedPuzzle(
        title="Manual Word Search",
        prompt=prompt,
        print_payload=print_payload,
        play_payload=print_payload,
        solution_payload=solution_payload,
        hints=[
            f"There are {len(words)} words hidden in the grid.",
            "Trace each word in the grid to mark it found.",
            f"The words are: {', '.join(words)}.",
        ],
        metadata={
            "generator": "manual.word_search.v1",
            "manual": True,
            "word_count": len(words),
            "gimmes": {"mode": "found_words", "count": len(gimme_words)},
        },
    )


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


def generate(
    theme: str,
    difficulty: str,
    source_data: list[str] | dict | None = None,
    seed: str | None = None,
    _generation_options: dict[str, Any] | None = None,
) -> GeneratedPuzzle:
    generation_options = _generation_options or {}
    blocked_source = []
    if isinstance(source_data, dict):
        raw_words = source_data.get("words")
        source_words = raw_words if raw_words is not None else words_for_theme(theme, difficulty)
        blocked_source = source_data.get("blocked_words", [])
    else:
        source_words = source_data if source_data is not None else words_for_theme(theme, difficulty)
    blocked_words = {normalize_word(word) for word in blocked_source if len(normalize_word(word)) >= 3}
    candidate_words = [normalize_word(word) for word in source_words]
    candidate_words = list(dict.fromkeys(
        word
        for word in candidate_words
        if word and not any(blocked in word or blocked in word[::-1] for blocked in blocked_words)
    ))
    size = grid_size_option(generation_options, difficulty)
    candidate_words = [word for word in candidate_words if len(word) <= size]
    if not candidate_words:
        raise ValueError("Word search generation needs at least one allowed word that fits the selected grid size.")
    default_word_count = {"easy": 5, "medium": 7, "hard": 8}.get(difficulty, 5)
    max_word_count = integer_option(generation_options, "word_count_limit", default_word_count, 1, 30)
    if "word_count_min" in generation_options:
        min_word_count = integer_option(generation_options, "word_count_min", min(default_word_count, max_word_count), 1, 30)
    else:
        min_word_count = min(max_word_count, len(candidate_words))
    min_word_count = min(min_word_count, max_word_count)
    available_max = min(max_word_count, len(candidate_words))
    if available_max < min_word_count:
        raise ValueError(
            f"Word search generation needs at least {min_word_count} allowed words that fit the selected grid size."
        )
    generation_seed = seed or f"{theme}:{difficulty}:{','.join(candidate_words)}"
    rng = random.Random(generation_seed)
    word_count = rng.randint(min_word_count, available_max)
    shuffled_words = candidate_words[:]
    rng.shuffle(shuffled_words)
    words = shuffled_words[:word_count]
    allow_crossings = bool_option(generation_options, "allow_crossings", True)
    auto_confirm = bool_option(generation_options, "auto_confirm", True)
    direction_preset, directions = direction_options(generation_options)
    print_style = str(generation_options.get("print_style", "boxed")).strip().lower()
    if print_style not in {"boxed", "compact"}:
        print_style = "boxed"
    grid = [["" for _ in range(size)] for _ in range(size)]
    placements: list[dict] = []

    for word in sorted(words, key=len, reverse=True):
        if len(word) > size:
            raise ValueError(f"Word '{word}' is longer than the selected grid size.")
        placed = False
        for _ in range(500):
            dx, dy = rng.choice(directions)
            min_x, max_x = coordinate_bounds(len(word), dx, size)
            min_y, max_y = coordinate_bounds(len(word), dy, size)
            x = rng.randint(min_x, max_x)
            y = rng.randint(min_y, max_y)
            coords = [(x + i * dx, y + i * dy) for i in range(len(word))]
            if allow_crossings:
                can_place = all(grid[cy][cx] in {"", letter} for (cx, cy), letter in zip(coords, word))
            else:
                can_place = all(grid[cy][cx] == "" for cx, cy in coords)
            if can_place:
                for (cx, cy), letter in zip(coords, word):
                    grid[cy][cx] = letter
                placements.append({"word": word, "start": [x, y], "direction": [dx, dy]})
                placed = True
                break
        if not placed:
            raise ValueError(f"Unable to place word: {word}")

    grid = fill_grid(grid, rng, blocked_words)
    default_gimme_count = len(gimme_words_for_difficulty(words, difficulty))
    gimme_count = integer_option(generation_options, "gimme_count", default_gimme_count, 0, max(0, len(words) - 1))
    gimme_words = words[:gimme_count]
    gimme_cells = gimme_cells_for_placements(placements, gimme_words)

    settings = {
        "grid_size": size,
        "directions": direction_preset,
        "allow_crossings": allow_crossings,
        "auto_confirm": auto_confirm,
        "word_count_min": min_word_count,
        "word_count": len(words),
        "word_count_limit": max_word_count,
        "word_count_max": max_word_count,
        "print_style": print_style,
    }

    prompt = "Find all listed words."
    print_payload = {
        "kind": "word_search",
        "grid": ["".join(row) for row in grid],
        "words": words,
        "gimmes": {"mode": "found_words", "words": gimme_words, "cells": gimme_cells},
        "settings": settings,
    }
    solution_payload = {
        "words": words,
        "placements": placements,
        "gimmes": {"mode": "found_words", "words": gimme_words},
        "settings": settings,
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
            "gimmes": {"mode": "found_words", "count": len(gimme_words)},
            "generation_options": settings | {"gimme_count": len(gimme_words)},
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
        collaboration=collaboration,
        preview_builder=build_preview,
        description="A printable and interactive word-search grid.",
        supports_print=True,
        supports_web_play=True,
        source_requirements=("word_bank",),
        print_layout=PrintLayout(
            slug="word_search",
            sheet_class="print-layout-word-search",
            instructions="Find all listed words.",
            notes=("Supports boxed grids for pencil solving and compact letters for narrow receipts.",),
        ),
        supports_gimmes=True,
        gimme_modes=("found_words",),
        manual_builder=build_manual_puzzle,
        manual_editor_fields=(
            GenerationField(
                name="grid",
                label="Letter grid",
                field_type="textarea",
                required=True,
                help_text="Paste one square row per line. Spaces are ignored.",
                default="",
            ),
            GenerationField(
                name="words",
                label="Words",
                field_type="textarea",
                required=True,
                help_text="One word per line, or separate words with commas.",
                default="",
            ),
            GenerationField(
                name="auto_confirm",
                label="Auto-confirm found words",
                field_type="select",
                default="yes",
                options=(
                    {"value": "yes", "label": "Yes"},
                    {"value": "no", "label": "No"},
                ),
            ),
            GenerationField(
                name="gimme_count",
                label="Gimme words",
                field_type="number",
                default="0",
                help_text="Number of listed words already marked found.",
                minimum=0,
                maximum=30,
            ),
            GenerationField(
                name="print_style",
                label="Print style",
                field_type="select",
                default="boxed",
                options=(
                    {"value": "boxed", "label": "Boxed grid"},
                    {"value": "compact", "label": "Compact letters"},
                ),
            ),
        ),
        generation_fields=(
            GenerationField(
                name="grid_size",
                label="Grid size",
                field_type="select",
                default="difficulty_default",
                help_text="Use the difficulty default, pick a common size, or choose custom.",
                options=(
                    {"value": "difficulty_default", "label": "Use difficulty default"},
                    {"value": "8", "label": "8x8"},
                    {"value": "10", "label": "10x10"},
                    {"value": "12", "label": "12x12"},
                    {"value": "14", "label": "14x14"},
                    {"value": "custom", "label": "Custom"},
                ),
            ),
            GenerationField(
                name="custom_grid_size",
                label="Custom grid size",
                field_type="number",
                default="",
                help_text="Only used when Grid size is Custom.",
                minimum=6,
                maximum=24,
            ),
            GenerationField(
                name="directions",
                label="Allowed directions",
                field_type="select",
                default="horizontal_vertical_diagonal",
                options=(
                    {"value": "horizontal_vertical", "label": "Horizontal and vertical"},
                    {"value": "horizontal_vertical_diagonal", "label": "Horizontal, vertical, diagonal"},
                    {"value": "all", "label": "Horizontal, vertical, diagonal, backwards"},
                ),
            ),
            GenerationField(
                name="allow_crossings",
                label="Allow shared letters",
                field_type="select",
                default="yes",
                options=(
                    {"value": "yes", "label": "Yes"},
                    {"value": "no", "label": "No"},
                ),
            ),
            GenerationField(
                name="auto_confirm",
                label="Auto-confirm found words",
                field_type="select",
                default="yes",
                options=(
                    {"value": "yes", "label": "Yes"},
                    {"value": "no", "label": "No"},
                ),
            ),
            GenerationField(
                name="word_count_min",
                label="Minimum word count",
                field_type="number",
                default="",
                help_text="Optional. Leave blank to use as many as the maximum allows.",
                minimum=1,
                maximum=30,
            ),
            GenerationField(
                name="word_count_limit",
                label="Maximum word count",
                field_type="number",
                default="",
                help_text="Optional. Leave blank to let difficulty choose.",
                minimum=1,
                maximum=30,
            ),
            GenerationField(
                name="gimme_count",
                label="Gimme words",
                field_type="number",
                default="",
                help_text="Optional. Number of words already marked found.",
                minimum=0,
                maximum=30,
            ),
            GenerationField(
                name="print_style",
                label="Print style",
                field_type="select",
                default="boxed",
                options=(
                    {"value": "boxed", "label": "Boxed grid"},
                    {"value": "compact", "label": "Compact letters"},
                ),
            ),
        ),
    )
)
