# Puzzle Module Contract

Puzzary puzzle types are small Python modules under `app/puzzle_types/`. A module is discovered at startup when it calls `register_puzzle_type(...)`.

Modules are intentionally independent. Adding or removing a file should not require editing the generation wizard, print API, progress storage, or admin solution shell.

## Required Shape

Each module exports functions that match the `PuzzleType` contract:

```python
from app.puzzles import GeneratedPuzzle, GenerationField, PrintLayout, PuzzleType, register_puzzle_type


def build_source(source_entries, difficulty, excluded_tag_ids=None):
    return source_entries


def generate(theme, difficulty, source_data=None, seed=None, generation_options=None) -> GeneratedPuzzle:
    options = generation_options or {}
    return GeneratedPuzzle(
        title="Example Puzzle",
        prompt="Solve the example.",
        print_payload={"kind": "example"},
        play_payload={"kind": "example"},
        solution_payload={"answer": "EXAMPLE"},
        hints=["Try the obvious answer."],
        metadata={"generation_options": options},
    )


def build_manual_puzzle(manual_options) -> GeneratedPuzzle:
    return GeneratedPuzzle(
        title="Manual Example",
        prompt="Solve the manually-authored example.",
        print_payload={"kind": "example"},
        play_payload={"kind": "example"},
        solution_payload={"answer": "EXAMPLE"},
        hints=[],
        metadata={"manual": True},
    )


def check_answer(answer, solution_payload):
    is_correct = answer.strip().upper() == solution_payload["answer"]
    return is_correct, 1 if is_correct else 0, 1


def normalize_progress(payload, solution_payload):
    return payload if isinstance(payload, dict) else {}


def count_progress(progress_payload, solution_payload):
    return 0, 1


register_puzzle_type(
    PuzzleType(
        slug="example",
        label="Example",
        generator=generate,
        source_builder=build_source,
        answer_checker=check_answer,
        progress_normalizer=normalize_progress,
        progress_counter=count_progress,
        description="Short admin-facing description.",
        source_requirements=(),
        theme_mode="free_text",
        supports_print=True,
        supports_web_play=True,
        digital_only=False,
        print_layout=PrintLayout(
            slug="example",
            sheet_class="print-layout-example",
            instructions="Print-facing instructions.",
        ),
        generation_fields=(
            GenerationField(
                name="answer_count",
                label="Answer count",
                field_type="number",
                default=5,
                minimum=1,
                maximum=20,
            ),
        ),
        manual_builder=build_manual_puzzle,
        manual_editor_fields=(
            GenerationField(
                name="answer",
                label="Answer",
                required=True,
            ),
        ),
    )
)
```

## Lifecycle

1. Puzzary imports every non-private file in `app/puzzle_types/`.
2. The module registers a `PuzzleType`.
3. The generation wizard reads `generation_fields`, `source_requirements`, and `theme_mode`.
4. Puzzary validates submitted generation options against the declared fields.
5. The module's `source_builder` receives already-filtered source data.
6. The module's `generator` returns a `GeneratedPuzzle`.
7. Puzzary stores the generated play, print, solution, hints, and metadata payloads.
8. Web play, print rendering, answer checking, progress counting, reveals, and admin solution views use the registered contract.

## Enabling and Disabling

Registered modules can be disabled from **Admin -> Settings**. A disabled module:

- is hidden from the public and admin generation wizards;
- is rejected by `/api/puzzles/generate`;
- remains available for existing puzzles, QR links, print pages, progress, and admin solution views.

## Generation Fields

Use `GenerationField` for module-specific options. Supported field types are:

- `text`
- `number`
- `select`
- `checkboxes`

Puzzary serializes these fields into `/api/puzzle-types` and the generation wizard. It also validates required fields, allowed select/checkbox values, and numeric min/max limits before calling the module generator.

## Manual Editor Helpers

Manual puzzle creation always supports raw payload editing. A module can also provide a GUI helper by setting:

- `manual_editor_fields`: a tuple of `GenerationField` values rendered in **Admin -> Puzzles -> Manual Puzzle**.
- `manual_builder`: a function that receives the submitted helper options and returns a `GeneratedPuzzle`.

The helper should build the same `print_payload`, `play_payload`, `solution_payload`, hints, and metadata shape used by generated puzzles. Puzzary stores the result as a manual puzzle draft or published puzzle, depending on the admin's selected save mode.

Supported manual field types are the generation field types plus `textarea`.

## Payload Responsibilities

Use separate payloads for separate audiences:

- `play_payload`: browser interaction state, with no hidden answers unless the game intentionally needs them client-side.
- `print_payload`: stable paper layout data for `/puzzles/{id}/print` and `/puzzles/{id}/print/render`.
- `solution_payload`: answer key, placements, reveal data, and admin-only solution details.
- `metadata`: generation settings, seed, source references, and module-specific notes.

## Templates

Optional templates use the module slug:

- `app/templates/puzzle_types/{slug}.html`
- `app/templates/puzzle_solutions/{slug}.html`

Without a custom play template, Puzzary falls back to a simple prompt and answer form. Without a custom solution template, admins see the raw solution payload.

## Print Support

Modules can provide `app/templates/puzzle_print/{slug}.html` for custom print rendering without editing the central print dispatcher. It receives `puzzle`, `print_payload`, and `print_layout`; include necessary module CSS in the fragment so immutable issue snapshots preserve it.

For a manual canvas editor, declare `manual_editor_script` as a static asset filename. Register `window.PuzzaryManualEditors[slug] = (form, parts) => { ... }` in that script. `parts` supplies the toolbar, canvas, and properties regions; synchronize the declared `manual_*` form fields. The admin loads registered module editor assets automatically.

Set `supports_print=True` and provide a `PrintLayout`. If layout details depend on generated payload data, provide `print_layout_builder`.

Every print-enabled module automatically participates in:

- `/puzzles/{id}/print`
- `/puzzles/{id}/print/render`
- `/api/puzzles/{id}/print-preview`

## Module Metadata API

Authorized API clients can inspect the current module contract:

```text
GET /api/puzzle-types
GET /api/puzzle-types?include_disabled=true
```

The response includes each module's label, enabled state, source requirements, generation fields, manual editor fields, print layout, gimme support, and declared templates.
