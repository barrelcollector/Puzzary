# Puzzle Module Contract

Puzzary puzzle types are small Python modules that call
`register_puzzle_type(...)` at startup. Core modules live under
`app/puzzle_types/`; independently released packages live in
`PUZZARY_PLUGIN_DIR` (default `/data/plugins`).

## External Packages

An installed package is one directory containing `puzzary-plugin.json`:

```json
{
  "schema": "puzzary.plugin.v1",
  "api_version": "1",
  "slug": "example",
  "name": "Example",
  "version": "1.0.0",
  "entrypoint": "plugin.py",
  "templates": "templates",
  "static": "static",
  "replaces_builtin": false
}
```

The entrypoint registers exactly one puzzle type matching `slug`. Templates use
`puzzle_types/{slug}.html`, `puzzle_solutions/{slug}.html`, and
`puzzle_print/{slug}.html` inside the declared template folder. Static files are
served at `/plugins/{slug}/static/`; `play_script` may use an absolute path below
that URL.

Packages execute trusted server-side Python. Install only reviewed, versioned
release archives, pin their published SHA-256 digest, and restart Puzzary after
an install or upgrade. `scripts/install_plugin.py` validates the digest, rejects
unsafe archive paths, stages the package, and retains the previous package as a
rollback directory. Docker persists packages within the existing `./data`
volume. Admin API clients can inspect loaded packages and startup errors at
`GET /api/plugin-packages`.

For a private GitHub repository, set `PUZZARY_PLUGIN_GITHUB_TOKEN` only in the
installer process environment. The token is sent as an authorization header and
is never stored in the installed package or passed on the command line.

The official packages and reference Maze package are maintained at
https://github.com/barrelcollector/puzzary-official-plugins.

## Contributor workflow

1. Create `app/puzzle_types/<slug>.py`; keep the slug stable because saved
   puzzles use it as their type identifier.
2. Implement generation or manual-building behavior and register one
   `PuzzleType` as described below.
3. Keep player-safe, printable, and secret answer data in separate payloads.
4. Add optional web, print, solution, editor, and static assets using the
   module slug and the paths documented below.
5. Add a focused `scripts/verify_<slug>.py` regression check. Use a temporary
   SQLite database or another disposable database, never a shared deployment.
6. Run the focused check, Python compilation, and `git diff --check`. If the
   module supports Group Solve, also run `scripts/verify_all_group_puzzles.py`.

From the repository root with a virtual environment installed:

```bash
PYTHONPATH=. python scripts/verify_<slug>.py
PYTHONPATH=. python -m compileall -q app scripts/verify_<slug>.py
git diff --check
```

Windows PowerShell users can set `$env:PYTHONPATH = "."` and invoke
`.venv\\Scripts\\python.exe` with the same script arguments.

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
2. It validates and imports packages from `PUZZARY_PLUGIN_DIR`. A package may
   explicitly replace a built-in type while keeping the same saved-data slug.
3. The module registers a `PuzzleType`.
4. The generation wizard reads `generation_fields`, `source_requirements`, and `theme_mode`.
5. Puzzary validates submitted generation options against the declared fields.
6. The module's `source_builder` receives already-filtered source data.
7. The module's `generator` returns a `GeneratedPuzzle`.
8. Puzzary stores the generated play, print, solution, hints, and metadata payloads.
9. Web play, print rendering, answer checking, progress counting, reveals, and admin solution views use the registered contract.

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

- `play_payload`: public browser interaction state. It must never contain hidden answers, placements, solution coordinates, or other data that can reconstruct the answer. Intentional givens belong here.
- `print_payload`: stable paper layout data for `/puzzles/{id}/print` and `/puzzles/{id}/print/render`.
- `solution_payload`: server-only answer key, placements, reveal data, and admin-only solution details. Player templates, page source, and ordinary puzzle API responses must not serialize it.
- `metadata`: generation settings, seed, source references, and module-specific notes.

Interactive checks, hints, and reveals must use the module's server-side `collaboration` action handler. Normal checks may report completion, but must not return per-cell correctness or partial match counts. Explicit hint and reveal actions may return only the answer material the player requested. Public puzzle rules, such as a duplicate in a Sudoku row, may still be validated in the browser because they do not require the stored solution.

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
GET /api/plugin-packages
```

The response includes each module's label, enabled state, source requirements, generation fields, manual editor fields, print layout, gimme support, and declared templates.
