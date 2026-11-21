# Puzzle Module Contract

Puzzary puzzle types are independently released packages that call
`register_puzzle_type(...)` at startup. Installed packages live in
`PUZZARY_PLUGIN_DIR` (default `/data/plugins`). Core owns the package API and
shared services, but no canonical puzzle implementation or puzzle-specific
asset fallback.

## External Packages

An installed package is one directory containing `puzzary-plugin.json`:

```json
{
  "schema": "puzzary.plugin.v1",
  "api_version": "1",
  "slug": "example",
  "name": "Example",
  "version": "1.0.0",
  "author": "Example Author",
  "description": "Adds Example puzzles.",
  "license": "Apache-2.0",
  "min_core_version": "0.2.1",
  "max_core_version": "1.0.0",
  "capabilities": ["puzzles.generate", "puzzles.play", "puzzles.print"],
  "requires": [],
  "settings": [
    {"name": "endpoint", "label": "Service URL", "type": "text", "required": true},
    {"name": "api_key", "label": "API key", "type": "secret"}
  ],
  "entrypoint": "plugin.py",
  "templates": "templates",
  "static": "static"
}
```

Content-only packages use the same installer and checksum protections but do
not execute Python. They declare `"package_type": "content"`, omit
`entrypoint`, and provide one or more validated `content_packs` entries with an
id, kind, directory, and manifest path. Core exposes their resolved installed
paths through the package status API. A missing content package is reported;
core never substitutes a stale bundled copy.

The official English Dictionary Content package is the canonical example. Its
data, build tools, verification history, and third-party license notices live at
https://github.com/barrelcollector/puzzary-official-plugins/tree/main/plugins/dictionary_content.

The entrypoint registers exactly one puzzle type matching `slug`. Templates use
`puzzle_types/{slug}.html`, `puzzle_solutions/{slug}.html`, and
`puzzle_print/{slug}.html` inside the declared template folder. Static files are
served at `/plugins/{slug}/static/`; `play_script` may use an absolute path below
that URL.

Packages execute trusted server-side Python with the same process privileges as
Puzzary. Capability declarations are reviewed metadata, not a sandbox. Install
only reviewed, versioned release archives and pin their published SHA-256
digest. **Admin -> Plugins** is
the primary management surface. It shows whether each module is loaded from an
installed package or is unavailable, installs one or all
official modules, gives each module its own update and enable/disable controls,
accepts a custom GitHub release URL or local ZIP plus digest, renders declared
configuration fields, reports load errors, supports recoverable uninstall, and
manages generation availability. A restart activates newly installed
code; running requests are never hot-swapped.

The same operations are available for automation through
`GET /api/plugin-packages`, `GET /api/plugin-packages/catalog`,
`GET`, `PUT`, and `DELETE /api/plugin-packages/github-access`,
`POST /api/plugin-packages/github-access/test`,
`POST /api/plugin-packages/install`, `POST /api/plugin-packages/install-upload`,
`POST /api/plugin-packages/install-official`, `PATCH` and `DELETE
/api/plugin-packages/{slug}`, `GET` and `PUT
/api/plugin-packages/{slug}/settings`, and `POST
/api/plugin-packages/{slug}/rollback`. The web and API paths share the same
validation, audit, permission, staging, and rollback behavior. The optional
`scripts/install_plugin.py` command uses that installer for recovery or scripted
deployment. Docker persists packages within the existing `./data` volume.

An account with `plugins.manage` can install or upgrade one through the API:

```json
{
  "url": "https://github.com/owner/repository/releases/download/v1.0.0/puzzary-example-1.0.0.zip",
  "sha256": "<published 64-character digest>"
}
```

The response identifies the installed version and sets `restart_required` to
`true`.

Local upload is a multipart request containing `package`, `sha256`, and the
explicit `trust_package=true` acknowledgement. Both download paths enforce a
100 MB archive limit, a 250 MB expanded limit, safe ZIP paths, exact digest,
manifest schema and API compatibility, core version bounds, declared
capabilities, and installed dependencies before atomically replacing a package.

Supported configuration field types are `text`, `textarea`, `number`,
`checkbox`, `select`, and `secret`. Select fields declare `options`; secret
fields cannot package a default. Settings are validated on the server and the
complete configuration is encrypted at rest. Secret values are never returned
by the web page or API. Plugin code reads its current values with:

```python
from app.plugin_settings import configured_plugin_settings

settings = configured_plugin_settings("example")
```

Uninstall moves the current package to its rollback slot instead of deleting
it. Saved settings, generated puzzles, progress, and other database records are
preserved. The administrator can restore the package from the rollback control;
install, update, uninstall, and restore all require an application restart to
change the code currently loaded in memory. A failing plugin is isolated during
startup and shown under Plugin Load Errors without preventing other modules from
loading.

For a private GitHub repository, save a fine-grained GitHub access token under
**Admin -> Plugins -> Private GitHub Access**. The field is write-only: Puzzary
encrypts the value with the server session key and never returns or displays it
after submission. The page can test, replace, or remove the saved credential.
The supporting API accepts the credential only in the `PUT` request body and
returns redacted status fields. `PUZZARY_PLUGIN_GITHUB_TOKEN` remains an optional
server-environment fallback for recovery and existing deployments.

Independently installable releases for all ten official puzzle types and shared
content packages are
publicly maintained at https://github.com/barrelcollector/puzzary-official-plugins.
Official modules install and update without a GitHub credential; private access
is only needed for additional modules hosted in private repositories. A fresh
core installation intentionally has no puzzle implementation until packages are
installed from **Admin -> Plugins**. Existing puzzle records remain preserved
and show an explicit unavailable-module message if their package is missing.

## Contributor workflow

1. Create `plugins/<slug>/puzzary-plugin.json` and `plugin.py` in the module
   repository; keep the slug stable because saved puzzles use it as their type
   identifier.
2. Implement generation or manual-building behavior and register one
   `PuzzleType` as described below.
3. Keep player-safe, printable, and secret answer data in separate payloads.
4. Add optional web, print, solution, editor, and static assets using the
   module slug and the paths documented below.
5. Add focused package validation and, when core behavior is involved, a
   `scripts/verify_<slug>.py` regression check. Use a disposable database.
6. Run package builds, the focused check, Python compilation, and
   `git diff --check`. If the
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

1. Puzzary validates packages from `PUZZARY_PLUGIN_DIR`.
2. Puzzle package entrypoints register exactly one `PuzzleType` matching their
   manifest slug; content-only packages declare validated content pack paths and
   execute no entrypoint.
3. Missing or incompatible packages are reported without loading a fallback.
4. The generation wizard reads `generation_fields`, `source_requirements`, and `theme_mode`.
5. Puzzary validates submitted generation options against the declared fields.
6. The module's `source_builder` receives already-filtered source data.
7. The module's `generator` returns a `GeneratedPuzzle`.
8. Puzzary stores the generated play, print, solution, hints, and metadata payloads.
9. Web play, print rendering, answer checking, progress counting, reveals, and admin solution views use the registered contract.

## Enabling and Disabling

Registered modules can be disabled from **Admin -> Plugins**. A disabled module:

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
GET /api/plugin-packages/catalog
POST /api/plugin-packages/install
POST /api/plugin-packages/install-upload
POST /api/plugin-packages/install-official
PATCH /api/plugin-packages/{slug}
DELETE /api/plugin-packages/{slug}
GET /api/plugin-packages/{slug}/settings
PUT /api/plugin-packages/{slug}/settings
POST /api/plugin-packages/{slug}/rollback
```

The response includes each module's label, enabled state, source requirements, generation fields, manual editor fields, print layout, gimme support, and declared templates.
