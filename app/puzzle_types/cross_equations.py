from __future__ import annotations

import json
import random
from itertools import product
from typing import Any

from app.puzzles import GeneratedPuzzle, GenerationField, PuzzleType, register_puzzle_type


DigitGrid = list[list[int]]
OpGrid = list[list[str]]


DIGIT_COORDS = [(x, y) for y in (0, 2, 4) for x in (0, 2, 4)]


def build_source(_source_entries: Any, _difficulty: str, _excluded_tag_ids: set[int] | None = None) -> dict:
    return {}


def apply_operator(
    left: int,
    right: int,
    operator: str,
    result_min: int = 1,
    result_max: int = 9,
    whole_number_division: bool = True,
) -> int | None:
    if operator == "+":
        value = left + right
    elif operator == "-":
        value = left - right
    elif operator == "x":
        value = left * right
    elif operator == "/":
        if right == 0:
            return None
        if whole_number_division:
            if left % right != 0:
                return None
            value = left // right
        else:
            quotient = left / right
            if quotient != int(quotient):
                return None
            value = int(quotient)
    else:
        return None
    return value if result_min <= value <= result_max else None


def allowed_operators(difficulty: str, operator_set: str = "", allow_division: bool = True) -> list[str]:
    if operator_set == "add_subtract":
        return ["+", "-"]
    if operator_set == "add_subtract_multiply":
        return ["+", "-", "x"]
    if operator_set == "all":
        return ["+", "-", "x", "/"] if allow_division else ["+", "-", "x"]
    if difficulty == "hard":
        return ["+", "-", "x", "/"] if allow_division else ["+", "-", "x"]
    if difficulty == "medium":
        return ["+", "-", "x"]
    return ["+", "-"]


def find_solution(
    rng: random.Random,
    difficulty: str,
    operator_set: str = "",
    number_max: int = 9,
    result_min: int = 1,
    result_max: int = 9,
    allow_division: bool = True,
    whole_number_division: bool = True,
) -> tuple[DigitGrid, OpGrid]:
    operators = allowed_operators(difficulty, operator_set, allow_division)
    fallback_digits = [[1, 2, 3], [3, 1, 4], [4, 3, 7]]
    fallback_ops = [["+", "+", "+"], ["+", "", "+"], ["+", "+", "+"]]

    for _attempt in range(12000):
        a, b, d, e = [rng.randint(1, number_max) for _ in range(4)]
        row_top, row_middle, row_bottom = [rng.choice(operators) for _ in range(3)]
        col_left, col_middle, col_right = [rng.choice(operators) for _ in range(3)]

        c = apply_operator(a, b, row_top, result_min, result_max, whole_number_division)
        f = apply_operator(d, e, row_middle, result_min, result_max, whole_number_division)
        g = apply_operator(a, d, col_left, result_min, result_max, whole_number_division)
        h = apply_operator(b, e, col_middle, result_min, result_max, whole_number_division)
        if None in (c, f, g, h):
            continue

        i_from_row = apply_operator(g, h, row_bottom, result_min, result_max, whole_number_division)
        i_from_col = apply_operator(c, f, col_right, result_min, result_max, whole_number_division)
        if i_from_row is None or i_from_row != i_from_col:
            continue

        return (
            [[a, b, c], [d, e, f], [g, h, i_from_row]],
            [[row_top, row_middle, row_bottom], [col_left, col_middle, col_right]],
        )

    return fallback_digits, fallback_ops


def derive_solution(
    a: int,
    b: int,
    d: int,
    e: int,
    ops: OpGrid,
    result_min: int = 1,
    result_max: int = 9,
    whole_number_division: bool = True,
) -> DigitGrid | None:
    c = apply_operator(a, b, ops[0][0], result_min, result_max, whole_number_division)
    f = apply_operator(d, e, ops[0][1], result_min, result_max, whole_number_division)
    g = apply_operator(a, d, ops[1][0], result_min, result_max, whole_number_division)
    h = apply_operator(b, e, ops[1][1], result_min, result_max, whole_number_division)
    if None in (c, f, g, h):
        return None

    i_from_row = apply_operator(g, h, ops[0][2], result_min, result_max, whole_number_division)
    i_from_col = apply_operator(c, f, ops[1][2], result_min, result_max, whole_number_division)
    if i_from_row is None or i_from_row != i_from_col:
        return None

    return [[a, b, c], [d, e, f], [g, h, i_from_row]]


def solution_matches_givens(solution: DigitGrid, givens: dict[str, str]) -> bool:
    return all(str(solution[int(y) // 2][int(x) // 2]) == str(value) for key, value in givens.items() for x, y in [key.split(",", 1)])


def count_solutions(
    ops: OpGrid,
    givens: dict[str, str],
    number_max: int = 9,
    result_min: int = 1,
    result_max: int = 9,
    whole_number_division: bool = True,
    limit: int = 2,
) -> int:
    found = 0
    for a, b, d, e in product(range(1, number_max + 1), repeat=4):
        solution = derive_solution(a, b, d, e, ops, result_min, result_max, whole_number_division)
        if solution is None or not solution_matches_givens(solution, givens):
            continue
        found += 1
        if found >= limit:
            return found
    return found


def find_unique_puzzle(
    rng: random.Random,
    difficulty: str,
    operator_set: str = "",
    requested_given_count: int | None = None,
    number_max: int = 9,
    result_min: int = 1,
    result_max: int = 9,
    allow_division: bool = True,
    whole_number_division: bool = True,
) -> tuple[DigitGrid, OpGrid, dict[str, str]]:
    fallback_digits = [[1, 2, 3], [3, 1, 4], [4, 3, 7]]
    fallback_ops = [["+", "+", "+"], ["+", "", "+"], ["+", "+", "+"]]

    for _attempt in range(12000):
        solution, ops = find_solution(
            rng,
            difficulty,
            operator_set,
            number_max,
            result_min,
            result_max,
            allow_division,
            whole_number_division,
        )
        givens = given_cells(solution, difficulty, rng, requested_given_count)
        if count_solutions(ops, givens, number_max, result_min, result_max, whole_number_division) == 1:
            return solution, ops, givens

    fallback_givens = {
        "0,0": "1",
        "2,0": "2",
        "0,2": "3",
        "2,2": "1",
        "4,2": "4",
    }
    return fallback_digits, fallback_ops, fallback_givens


def given_cells(
    solution: DigitGrid,
    difficulty: str,
    rng: random.Random,
    requested_count: int | None = None,
) -> dict[str, str]:
    given_count = requested_count or {"easy": 4, "medium": 3, "hard": 2}.get(difficulty, 3)
    given_count = max(2, min(8, given_count))
    coords = DIGIT_COORDS[:]
    rng.shuffle(coords)
    return {f"{x},{y}": str(solution[y // 2][x // 2]) for x, y in coords[:given_count]}


def symbol_cell(value: str) -> dict:
    return {"type": "symbol", "value": value}


def spacer_cell() -> dict:
    return {"type": "spacer", "value": ""}


def number_cell(x: int, y: int, solution: DigitGrid, givens: dict[str, str], include_solution: bool = False) -> dict:
    key = f"{x},{y}"
    value = solution[y // 2][x // 2]
    cell = {
        "type": "number",
        "key": key,
        "value": int(givens[key]) if key in givens else 0,
        "given": key in givens,
    }
    if include_solution:
        cell["solution"] = value
    return cell


def build_cells(solution: DigitGrid, ops: OpGrid, givens: dict[str, str], include_solution: bool = False) -> list[list[dict]]:
    return [
        [
            number_cell(0, 0, solution, givens, include_solution),
            symbol_cell(ops[0][0]),
            number_cell(2, 0, solution, givens, include_solution),
            symbol_cell("="),
            number_cell(4, 0, solution, givens, include_solution),
        ],
        [symbol_cell(ops[1][0]), spacer_cell(), symbol_cell(ops[1][1]), spacer_cell(), symbol_cell(ops[1][2])],
        [
            number_cell(0, 2, solution, givens, include_solution),
            symbol_cell(ops[0][1]),
            number_cell(2, 2, solution, givens, include_solution),
            symbol_cell("="),
            number_cell(4, 2, solution, givens, include_solution),
        ],
        [symbol_cell("="), spacer_cell(), symbol_cell("="), spacer_cell(), symbol_cell("=")],
        [
            number_cell(0, 4, solution, givens, include_solution),
            symbol_cell(ops[0][2]),
            number_cell(2, 4, solution, givens, include_solution),
            symbol_cell("="),
            number_cell(4, 4, solution, givens, include_solution),
        ],
    ]


def equation_text(left: int, operator: str, right: int, result: int) -> str:
    return f"{left} {operator} {right} = {result}"


def equations(solution: DigitGrid, ops: OpGrid) -> list[dict]:
    return [
        {"number": 1, "direction": "across", "label": "Across 1", "cells": ["0,0", "2,0", "4,0"], "equation": equation_text(solution[0][0], ops[0][0], solution[0][1], solution[0][2])},
        {"number": 2, "direction": "across", "label": "Across 2", "cells": ["0,2", "2,2", "4,2"], "equation": equation_text(solution[1][0], ops[0][1], solution[1][1], solution[1][2])},
        {"number": 3, "direction": "across", "label": "Across 3", "cells": ["0,4", "2,4", "4,4"], "equation": equation_text(solution[2][0], ops[0][2], solution[2][1], solution[2][2])},
        {"number": 1, "direction": "down", "label": "Down 1", "cells": ["0,0", "0,2", "0,4"], "equation": equation_text(solution[0][0], ops[1][0], solution[1][0], solution[2][0])},
        {"number": 2, "direction": "down", "label": "Down 2", "cells": ["2,0", "2,2", "2,4"], "equation": equation_text(solution[0][1], ops[1][1], solution[1][1], solution[2][1])},
        {"number": 3, "direction": "down", "label": "Down 3", "cells": ["4,0", "4,2", "4,4"], "equation": equation_text(solution[0][2], ops[1][2], solution[1][2], solution[2][2])},
    ]


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
            return {f"{x},{y}": digit for (x, y), digit in zip(DIGIT_COORDS, digits, strict=False)}
    if not isinstance(value, dict):
        return {}
    answers = value.get("answers", value)
    if not isinstance(answers, dict):
        return {}
    normalized = {}
    for key, raw_value in answers.items():
        text = str(raw_value).strip()
        if str(key) and text.lstrip("-").isdigit():
            normalized[str(key)] = text
    return normalized


def expected_answers(solution_payload: dict) -> dict[str, str]:
    answers = solution_payload.get("answers", {})
    if not isinstance(answers, dict):
        return {}
    return {str(key): str(value) for key, value in answers.items()}


def check_answer(answer: str, solution_payload: dict) -> tuple[bool, int, int]:
    supplied = flat_answer_from_payload(answer)
    givens = {str(key): str(value) for key, value in solution_payload.get("givens", {}).items()}
    expected = expected_answers(solution_payload)
    supplied = {**givens, **supplied}
    matched = sum(1 for key, value in expected.items() if supplied.get(key) == value)
    return matched == len(expected), len(expected), matched


def normalize_progress(payload: Any, solution_payload: dict) -> dict:
    supplied = flat_answer_from_payload(getattr(payload, "answers", {}) if payload is not None else {})
    givens = {str(key): str(value) for key, value in solution_payload.get("givens", {}).items()}
    return {"answers": {key: value for key, value in supplied.items() if key not in givens}}


def count_progress(progress_payload: dict, solution_payload: dict) -> tuple[int, int]:
    expected = expected_answers(solution_payload)
    givens = {str(key): str(value) for key, value in solution_payload.get("givens", {}).items()}
    supplied = progress_payload.get("answers", {}) if isinstance(progress_payload, dict) else {}
    if not isinstance(supplied, dict):
        supplied = {}
    combined = {**givens, **{str(key): str(value) for key, value in supplied.items()}}
    matched = sum(1 for key, value in expected.items() if combined.get(key) == value)
    return matched, len(expected)


def integer_option(options: dict[str, Any], key: str) -> int | None:
    try:
        return int(str(options.get(key, "")).strip())
    except (TypeError, ValueError):
        return None


def bool_option(options: dict[str, Any], key: str, default: bool) -> bool:
    raw = str(options.get(key, "")).strip().lower()
    if raw in {"yes", "true", "1", "on"}:
        return True
    if raw in {"no", "false", "0", "off"}:
        return False
    return default


def ranged_integer_option(options: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = integer_option(options, key)
    if value is None:
        value = default
    return max(minimum, min(maximum, value))


def number_max_option(options: dict[str, Any]) -> int:
    value = str(options.get("number_range", "1-9")).strip()
    return {"1-9": 9, "1-12": 12, "1-20": 20}.get(value, 9)


def result_limits(options: dict[str, Any], number_max: int) -> tuple[int, int]:
    constraint = str(options.get("result_constraints", "positive_only")).strip().lower()
    if constraint == "allow_negative":
        return -number_max * number_max, number_max * number_max
    if constraint == "allow_zero":
        return 0, number_max * number_max
    return 1, number_max * number_max


def generate(
    theme: str,
    difficulty: str,
    _source_data: Any = None,
    seed: str | None = None,
    generation_options: dict[str, Any] | None = None,
) -> GeneratedPuzzle:
    generation_seed = seed or f"cross_equations:{theme}:{difficulty}"
    rng = random.Random(generation_seed)
    generation_options = generation_options or {}
    operator_set = str(generation_options.get("operator_set") or "").strip()
    if operator_set == "difficulty_default":
        operator_set = ""
    grid_size = ranged_integer_option(generation_options, "grid_size", 3, 3, 5)
    custom_grid_size = integer_option(generation_options, "custom_grid_size")
    if str(generation_options.get("grid_size", "")).strip().lower() == "custom" and custom_grid_size is not None:
        grid_size = custom_grid_size
    if grid_size != 3:
        raise ValueError("Cross Equations currently supports the 3x3 equation grid. Larger patterns are tracked for a future generator.")
    number_max = number_max_option(generation_options)
    result_min, result_max = result_limits(generation_options, number_max)
    allow_division = bool_option(generation_options, "allow_division", True)
    whole_number_division = bool_option(generation_options, "whole_number_division", True)
    difficulty_style = str(generation_options.get("difficulty_style", "balanced")).strip().lower()
    if difficulty_style not in {"more_givens", "balanced", "harder_operators"}:
        difficulty_style = "balanced"
    if difficulty_style == "more_givens":
        requested_default = {"easy": 6, "medium": 5, "hard": 4}.get(difficulty, 5)
    else:
        requested_default = {"easy": 4, "medium": 3, "hard": 2}.get(difficulty, 3)
    given_count = integer_option(generation_options, "given_count") or requested_default
    if difficulty_style == "harder_operators" and not operator_set:
        operator_set = "all" if allow_division else "add_subtract_multiply"
    solution, ops, givens = find_unique_puzzle(
        rng,
        difficulty,
        operator_set,
        given_count,
        number_max,
        result_min,
        result_max,
        allow_division,
        whole_number_division,
    )
    answer_map = {f"{x},{y}": str(solution[y // 2][x // 2]) for x, y in DIGIT_COORDS}
    equation_list = equations(solution, ops)
    payload = {
        "kind": "cross_equations",
        "cells": build_cells(solution, ops, givens),
        "givens": givens,
        "equations": [{"number": item["number"], "direction": item["direction"], "label": item["label"], "cells": item["cells"]} for item in equation_list],
        "settings": {
            "grid_size": grid_size,
            "number_range": f"1-{number_max}",
            "allow_division": allow_division,
            "whole_number_division": whole_number_division,
            "result_min": result_min,
            "result_max": result_max,
            "difficulty_style": difficulty_style,
        },
    }
    solution_payload = {
        "answers": answer_map,
        "givens": givens,
        "cells": build_cells(solution, ops, answer_map, include_solution=True),
        "equations": equation_list,
    }
    blank_count = len(answer_map) - len(givens)
    title_prefix = theme.replace("_", " ").replace("-", " ").title().strip()
    title = f"{title_prefix} Cross Equations" if title_prefix else "Cross Equations"
    return GeneratedPuzzle(
        title=title,
        prompt="Fill the shared number cells so every across and down equation is true.",
        print_payload=payload,
        play_payload=payload,
        solution_payload=solution_payload,
        hints=[
            "Each row with numbers is an across equation.",
            "Each column with numbers is a down equation.",
            "A shared cell must satisfy both its across and down equation.",
            f"This puzzle starts with {len(givens)} givens and {blank_count} blanks.",
        ],
        metadata={
            "generator": "local.cross_equations.v1",
            "seed": generation_seed,
            "givens": len(givens),
            "blanks": blank_count,
            "operators": sorted({operator for row in ops for operator in row if operator}),
            "generation_options": {
                "operator_set": operator_set or "difficulty_default",
                "given_count": len(givens),
                "grid_size": grid_size,
                "number_range": f"1-{number_max}",
                "allow_division": allow_division,
                "whole_number_division": whole_number_division,
                "result_min": result_min,
                "result_max": result_max,
                "difficulty_style": difficulty_style,
            },
        },
    )


register_puzzle_type(
    PuzzleType(
        slug="cross_equations",
        label="Cross Equations",
        generator=generate,
        source_builder=build_source,
        answer_checker=check_answer,
        progress_normalizer=normalize_progress,
        progress_counter=count_progress,
        description="A crossing arithmetic grid where rows and columns share number cells.",
        supports_print=True,
        supports_web_play=True,
        theme_mode="none",
        generation_fields=(
            GenerationField(
                name="grid_size",
                label="Grid size",
                field_type="select",
                default="3",
                help_text="The current generator supports the 3x3 equation grid. Larger layouts are planned.",
                options=(
                    {"value": "3", "label": "3x3 equation grid"},
                    {"value": "custom", "label": "Custom"},
                ),
            ),
            GenerationField(
                name="custom_grid_size",
                label="Custom grid size",
                field_type="number",
                default="",
                help_text="Currently must be 3.",
                minimum=3,
                maximum=5,
            ),
            GenerationField(
                name="operator_set",
                label="Operators",
                field_type="select",
                default="difficulty_default",
                help_text="Choose which math operators can appear in the grid.",
                options=(
                    {"value": "difficulty_default", "label": "Use difficulty default"},
                    {"value": "add_subtract", "label": "Add and subtract"},
                    {"value": "add_subtract_multiply", "label": "Add, subtract, multiply"},
                    {"value": "all", "label": "Add, subtract, multiply, divide"},
                ),
            ),
            GenerationField(
                name="number_range",
                label="Number range",
                field_type="select",
                default="1-9",
                options=(
                    {"value": "1-9", "label": "1-9"},
                    {"value": "1-12", "label": "1-12"},
                    {"value": "1-20", "label": "1-20"},
                ),
            ),
            GenerationField(
                name="allow_division",
                label="Allow division",
                field_type="select",
                default="yes",
                options=(
                    {"value": "yes", "label": "Yes"},
                    {"value": "no", "label": "No"},
                ),
            ),
            GenerationField(
                name="whole_number_division",
                label="Whole-number division",
                field_type="select",
                default="yes",
                options=(
                    {"value": "yes", "label": "Yes"},
                    {"value": "no", "label": "No"},
                ),
            ),
            GenerationField(
                name="result_constraints",
                label="Result constraints",
                field_type="select",
                default="positive_only",
                options=(
                    {"value": "positive_only", "label": "Positive only"},
                    {"value": "allow_zero", "label": "Allow zero"},
                    {"value": "allow_negative", "label": "Allow negative"},
                ),
            ),
            GenerationField(
                name="difficulty_style",
                label="Difficulty style",
                field_type="select",
                default="balanced",
                options=(
                    {"value": "more_givens", "label": "More givens"},
                    {"value": "balanced", "label": "Balanced"},
                    {"value": "harder_operators", "label": "Harder operators"},
                ),
            ),
            GenerationField(
                name="given_count",
                label="Starter numbers",
                field_type="number",
                default="",
                help_text="Optional. Leave blank to let difficulty choose; valid range is 2 to 8.",
                minimum=2,
                maximum=8,
            ),
        ),
    )
)
