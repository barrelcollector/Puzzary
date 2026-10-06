# Puzzary

Puzzary is a self-hosted puzzle library and answer portal for Daily Stripe.

It owns puzzle generation, stored solutions, hints, attempts, photo submissions, and QR-backed web pages. Home Assistant and Daily Stripe should call Puzzary through its API instead of storing puzzle state themselves.

## Current MVP

- FastAPI app
- PostgreSQL database
- Web portal
- Password-based user accounts
- Admin-managed account creation and password resets
- Puzzle library view
- Admin panel for archiving, deleting, and organizing puzzles
- Admin-managed dictionary, word banks, categories, and tags
- Canonical dictionary words with reusable properties and tags
- Word banks that reference canonical dictionary words without duplicating them
- Word-search puzzle generator
- Crossword puzzle generator
- Word-pyramid puzzle generator
- Puzzle hints
- Answer checking
- Account-based puzzle progress
- Interactive word-search highlighting
- Photo submission upload endpoint
- Printable puzzle payload API for Daily Stripe

## Run

Copy the sample environment:

```bash
cp .env.example .env
```

Start the stack:

```bash
docker compose up -d --build
```

Open:

```text
http://localhost:8790
```

For Cloudflare Tunnel or reverse-proxy use, configure the public URL that printed QR codes and API links should use. You can set it with `APP_BASE_URL` before the first run, or change it later in **Admin -> Settings** without rebuilding the container.

```bash
APP_BASE_URL=https://puzzary.example.com docker compose up -d --build
```

Admin panel:

```text
http://localhost:8790/admin
```

## Accounts

Puzzary does not allow public self-registration. Users sign in with their display name or email address and password.

Admins manage accounts from:

```text
http://localhost:8790/admin/users
```

Admins can create users, assign the Admin role, activate/deactivate accounts, reset passwords with one-time setup links, and revoke active sessions. Puzzary prevents demoting or deactivating the last active administrator.

Fresh installations and existing databases always have a default admin account:

```text
username: admin
password: admin
```

Change that password after first login. Existing installations are migrated in place; the oldest existing user still becomes an admin if no admin exists.

Admins can issue per-account API tokens from the Users page. Tokens are shown once, then stored only as hashes.

## API

API requests require a token for an active user account. Send it as either:

```text
Authorization: Bearer pzy_...
```

or:

```text
X-Puzzary-Api-Token: pzy_...
```

Generate a puzzle:

```bash
curl -X POST http://localhost:8790/api/puzzles/generate \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN" \
  --data '{"puzzle_type":"word_search","theme":"daily_stripe","difficulty":"easy"}'
```

Get a puzzle:

```bash
curl http://localhost:8790/api/puzzles/{puzzle_id} \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN"
```

Export or import portable puzzles:

```bash
curl http://localhost:8790/api/puzzles/{puzzle_id}/export \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN"

curl http://localhost:8790/api/puzzles/export?ids=abc123,def456 \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN"

curl -X POST http://localhost:8790/api/puzzles/import \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN" \
  --data '{
    "source_url":"https://puzzary.example.com/api/puzzles/export",
    "source_api_token":"REMOTE_PUZZARY_API_TOKEN",
    "dry_run":true,
    "duplicate_policy":"skip"
  }'
```

Portable puzzle manifests use `schema: "puzzary.puzzle.item.v1"` for one puzzle or `schema: "puzzary.puzzle.bundle.v1"` with an `items` array for bundles. Imports validate that the puzzle type is installed locally and never install executable plugin code from a remote source. Supported import sources include another Puzzary export endpoint, pasted JSON, uploaded JSON, Pastebin/raw URLs, GitHub raw files, GitHub repo files, and Gists.

Create or update a manual puzzle:

```bash
curl -X POST http://localhost:8790/api/puzzles/manual \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN" \
  --data '{
    "puzzle_type":"word_search",
    "title":"Manual Word Search",
    "theme":"manual",
    "difficulty":"manual",
    "print_payload":{"kind":"word_search"},
    "play_payload":{"kind":"word_search"},
    "solution_payload":{"kind":"word_search"},
    "metadata":{},
    "hints":["Look for the theme words."],
    "draft":true
  }'

curl -X PATCH http://localhost:8790/api/puzzles/{puzzle_id}/manual \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN" \
  --data '{"puzzle_type":"word_search","title":"Manual Word Search","print_payload":{"kind":"word_search"},"play_payload":{"kind":"word_search"},"solution_payload":{"kind":"word_search"},"draft":false}'
```

Manual puzzles can also be created in **Admin -> Puzzles -> Manual Puzzle**. Drafts are hidden from the library but can be opened from admin for preview. Publishing requires non-empty print, play, and solution payload objects whose `kind` matches the selected puzzle type.

Submit an answer:

```bash
curl -X POST http://localhost:8790/api/puzzles/{puzzle_id}/attempts \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN" \
  --data '{"answer":"MERIDIAN,PUZZLE,ZEBRA,PRINT,CLUE"}'
```

Save logged-in word-search progress:

```bash
curl -X POST http://localhost:8790/api/puzzles/{puzzle_id}/progress \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN" \
  --data '{"found_words":["MERIDIAN","PUZZLE"]}'
```

Dictionary and content-management endpoints:

```text
GET    /api/dictionary?search=cloud
GET    /api/dictionary/export
POST   /api/dictionary/import
GET    /api/dictionary/import-sources
POST   /api/dictionary/import-sources
POST   /api/dictionary/import-sources/{source_id}/sync
DELETE /api/dictionary/import-sources/{source_id}
POST   /api/dictionary
GET    /api/dictionary/{word_id}
PATCH  /api/dictionary/{word_id}
DELETE /api/dictionary/{word_id}

POST   /api/dictionary/{word_id}/definitions
PATCH  /api/dictionary/{word_id}/definitions/{definition_id}
DELETE /api/dictionary/{word_id}/definitions/{definition_id}

GET    /api/tags
POST   /api/tags
GET    /api/tags/{tag_id}
PATCH  /api/tags/{tag_id}
DELETE /api/tags/{tag_id}

GET    /api/word-banks
POST   /api/word-banks
GET    /api/word-banks/{bank_id}
PATCH  /api/word-banks/{bank_id}
DELETE /api/word-banks/{bank_id}
POST   /api/word-banks/{bank_id}/words
DELETE /api/word-banks/{bank_id}/words/{link_id}
```

Import from another Puzzary server, a GitHub repo or raw file, Pastebin, or a generic JSON/CSV/text URL:

```bash
curl -X POST http://localhost:8790/api/dictionary/import \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN" \
  --data '{
    "source_url":"https://github.com/example/puzzle-words",
    "source_type":"auto",
    "source_label":"Example Word Feed",
    "dry_run":true
  }'
```

Use `"dry_run": false` to apply the import after reviewing the summary. Existing words are merged into the canonical dictionary word. Duplicate definitions are skipped, while new definitions for existing words are preserved.

Export a dictionary for another Puzzary server:

```bash
curl http://localhost:8790/api/dictionary/export \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN"
```

Create a periodic Puzzary-to-Puzzary sync source:

```bash
curl -X POST http://localhost:8790/api/dictionary/import-sources \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN" \
  --data '{
    "name":"Main Puzzary Dictionary",
    "source_url":"https://puzzary.example.com",
    "source_type":"puzzary",
    "source_api_token":"REMOTE_PUZZARY_API_TOKEN",
    "sync_interval_hours":24,
    "dry_run":false,
    "sync_now":true
  }'
```

Smoke-test flow for external dictionary clients:

```bash
curl -X POST http://localhost:8790/api/tags \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN" \
  --data '{"name":"API Smoke","tag_type":"Source"}'

curl -X POST http://localhost:8790/api/dictionary \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN" \
  --data '{"word":"codex","display_text":"Codex","definitions":[{"definition":"A bound set of instructions or writings.","clues":["Instruction book"]}]}'

curl http://localhost:8790/api/dictionary?search=codex \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN"
```

## Design Notes

Puzzary is intentionally separate from Home Assistant and Daily Stripe:

- Home Assistant triggers and displays state.
- Daily Stripe renders and prints issues.
- Puzzary generates puzzle content, stores answers, checks attempts, and serves QR pages.

## Puzzle Types

Puzzle types are auto-discovered modules under `app/puzzle_types`.

Each type owns its generator, source adapter, answer checker, and progress logic. Browser UI lives in `app/templates/puzzle_types/{slug}.html`, with optional admin solution display in `app/templates/puzzle_solutions/{slug}.html`.

The full contributor contract is documented in [`docs/puzzle-modules.md`](docs/puzzle-modules.md). Admins can enable or disable modules from **Admin -> Settings**; disabled modules stay playable for existing puzzles but disappear from generation screens and API generation.

To add a type, drop a new Python file into `app/puzzle_types/`. Files whose names start with `_` are ignored. The module must call `register_puzzle_type(...)` at import time:

```python
from app.puzzles import GeneratedPuzzle, GenerationField, PrintLayout, PuzzleType, register_puzzle_type


def generate(theme, difficulty, source_data=None, seed=None, generation_options=None) -> GeneratedPuzzle:
    ...


def build_source(word_entries, difficulty, excluded_tag_ids=None):
    ...


def check_answer(answer, solution_payload):
    ...


def normalize_progress(payload, solution_payload):
    ...


def count_progress(progress_payload, solution_payload):
    ...


register_puzzle_type(
    PuzzleType(
        slug="example",
        label="Example",
        generator=generate,
        source_builder=build_source,
        answer_checker=check_answer,
        progress_normalizer=normalize_progress,
        progress_counter=count_progress,
        description="Short description shown to admins and future tooling.",
        supports_print=True,
        supports_web_play=True,
        digital_only=False,
        source_requirements=("word_bank",),
        print_layout=PrintLayout(
            slug="example",
            sheet_class="print-layout-example",
            instructions="Print-facing instructions for paper solving.",
            notes=("Optional review notes for print tools.",),
        ),
        supports_gimmes=False,
        gimme_modes=(),
        generation_fields=(
            GenerationField(
                name="answer_count",
                label="Answer count",
                field_type="number",
                default=5,
                minimum=1,
                maximum=20,
                help_text="Server-side validation uses these limits before generation runs.",
            ),
        ),
    )
)
```

Optional templates:

- `app/templates/puzzle_types/{slug}.html` for custom web play.
- `app/templates/puzzle_solutions/{slug}.html` for admin solution display.

Print support:

- `GeneratedPuzzle.print_payload` is the stable paper-rendering data for an individual generated puzzle.
- `PuzzleType.print_layout` describes how that module should be printed: CSS class, receipt/page sizing, visible instructions, review notes, and whether to include the online solve link.
- `GET /puzzles/{id}/print` shows a browser print preview.
- `GET /puzzles/{id}/print/render` shows the same layout without app chrome for external PDF/print renderers.
- **Download PDF** on the print page uses bundled Chromium to render the same layout, including shading, gimmes, and embedded QR codes.
- `GET /puzzles/{id}/print.pdf` downloads a PDF for signed-in users. `GET /api/puzzles/{id}/print.pdf` uses the account API token (Bearer authentication).
- PDF and HTML print routes accept `size=receipt|zebra|4x6|letter|a4|custom`, `width_mm`, `height_mm`, and `margin_mm`. Receipt/Zebra PDFs calculate their length from the content; fixed page sizes paginate as needed. Widths support 25-1000mm; explicit heights support 25-5000mm. Match the physical paper size and use 100% scaling when printing the PDF.
- `print-preview` includes `pdf_url` for clients that want to download the rendered PDF. This does not change printer feeding or cutter commands.
- `GET /api/puzzles/{id}/print-preview` returns the module layout metadata and print/render URLs for Home Assistant, Daily Stripe, or other clients.
- Printed layouts include a QR code by default. The QR points to `/q/{id}`, records a lightweight scan count, and redirects to the matching puzzle page.
- If the user is not logged in after scanning, the login flow preserves the puzzle URL and returns them there after sign-in.
- Set **Admin -> Settings -> Public base URL** so printed QR codes use your public HTTPS address instead of a LAN URL.

If a registered type has no web template, Puzzary falls back to the puzzle prompt and answer submission form. If it has no solution template, admins see the raw solution payload.

`word_entries` is the shared source format Puzzary passes to puzzle modules after applying archive, ban, and excluded-tag filters. Each item is a dict with `word` and `definition` keys; `definition` may be `None`.

Puzzle modules can also support gimmes: pre-solved cells, words, rows, or regions included in the generated payload. A module that supports them should set `supports_gimmes=True`, describe its `gimme_modes`, persist the gimme data in payloads, render it distinctly, and count it as solved in progress/checking logic. The current examples are pre-found Word Search words, pre-filled Crossword entries, and starting Word Pyramid rows.

## Publishing And Issues

Open **Admin -> Publishing** to create an issue. Add printable puzzles from the library and optional text sections (for notes, quotes, or other curated content), reorder them, and save a draft. **Save & preview** opens the combined print layout. **Publish issue** saves immutable print snapshots, embedded QR images, and CSS; source puzzle edits, archival, deletion, or plugin removal do not change the published paper content. To revise an issue, duplicate it as a new draft. Archive/restore keeps issue history without deleting it.

Issue previews and PDFs use the same size, width, height, and margin parameters as individual puzzle prints. Fixed paper sizes start each subsequent section on a new page; roll PDFs concatenate all sections and calculate the complete roll length. One bundle PDF can be submitted as one printer job. Feeding and cutting remain the print service's responsibility.

For a folded, left-bound booklet, choose **Booklet / PDF** on an issue or **Layout -> Folded booklet** in its print preview. Select the physical sheet size (Letter, A4, 4x6, or a custom fixed width/height), then download the booklet PDF. Each section becomes one folded page; PDF pages contain two logical pages per landscape sheet side in nesting order. Blank pages are added at the end to reach a multiple of four. Print the downloaded PDF at actual size, one PDF page per sheet, double-sided with **flip on short edge**; do not enable an additional booklet or two-up mode in the printer driver. Stack the sheets in output order and fold them together down the centre. The preview shows sequential logical pages; the PDF contains the imposed sheet order. Duplex preferences are embedded but the printer dialog may override them. Roll paper needs an explicit fixed height for booklet mode. Normal sequential and roll PDFs are unchanged.

Web and API issue PDF routes accept `print_mode=booklet`, for example `/api/issues/{id}/print.pdf?print_mode=booklet&size=letter` or `/api/issues/latest/print.pdf?print_mode=booklet&size=a4`. With no size specified, booklet mode defaults to Letter; standard mode retains its existing roll default. Published issue snapshots are not modified by choosing a print mode.

Authenticated API routes:

- `GET /api/issues`: non-archived issues; regular accounts see published issues only.
- `GET /api/issues/latest`: newest published, non-archived issue, including ordered modules and `pdf_url`.
- `GET /api/issues/latest/print.pdf?size=zebra`: download the latest published issue directly.
- `GET /api/issues/{id}`: issue metadata and ordered print payloads.
- `GET /api/issues/{id}/print.pdf?size=zebra`: combined PDF; accepts standard/custom paper parameters.
- `POST /api/issues`: admin draft creation.
- `PUT /api/issues/{id}`: admin draft editing; published issues return 409.
- `POST /api/issues/{id}/publish`: admin publication.

Create a draft with this JSON, using real puzzle IDs from your library:

```json
{
  "title": "Daily Stripe",
  "description": "Today's puzzles",
  "modules": [
    {"kind": "puzzle", "puzzle_id": "PUZZLE_ID"},
    {"kind": "text", "title": "Quote", "body": "A curated quote.", "attribution": "Source"}
  ]
}
```

Use `Authorization: Bearer YOUR_ACCOUNT_API_TOKEN` for automation requests. Drafts and archived issues are visible only to administrators. A maximum of 20 sections is supported; roll PDF output is limited to five metres. Text sections accept plain text and preserve line breaks; embedded HTML is escaped. Future dedicated daily-content services can supply sections through this API.

Regression checks: `PYTHONPATH=. python scripts/verify_publishing.py` runs against a separate in-memory SQLite database and renders sample bundle PDFs under `/tmp/puzzary-publishing-qa`.

## Nonogram / Picross

Nonogram is a self-contained puzzle module with no dictionary or theme requirement. Generate square grids from 5x5 to 15x15 with mirrored, rotational, or random pixel patterns, optional starter cells, and a reproducible seed. OR-Tools CP-SAT proves uniqueness; additional fixed cells are added when clues alone have more than one answer. A timeout rejects generation rather than claiming uniqueness.

Web play supports fill, empty (X), and erase brushes, mouse/touch painting, right-click empty marks, arrow-key navigation, Space/Enter marking, X for empty, and Delete to erase. Fill strokes and keyboard fill leave X marks intact; Empty strokes also leave filled cells intact. Use Erase to remove either mark, or toggle a mark using its own tool. Progress belongs to the signed-in account. The generation/manual options **Auto-confirm completed rows** and **Auto-confirm completed columns** default to On: correct completed lines have green, crossed-out clues, and editing a line removes its confirmation. Newly confirmed lines play a progress chime, respecting the existing celebration mute preference; saved lines do not chime on reload. Off disables the corresponding automatic highlighting and chimes. Filling the picture correctly automatically completes the puzzle and triggers the existing celebration; Check still highlights incorrect marks. Empty cells do not all need X marks to complete, but incorrect X marks prevent completion.

Puzzle modules can call `window.PuzzarySounds?.progress()` for a short two-note progress chime or `window.PuzzarySounds?.smallWin()` for a three-note small win without opening the completion panel. `window.PuzzarySounds?.complete()` plays the full completion sound only; `window.PuzzaryCelebrate?.complete()` opens the celebration and plays that sound. These shared methods respect the same sound-mute preference, reuse an audio context, and tolerate unavailable or blocked browser audio. Call them on user-driven transitions, not when restoring progress on load.

Progress chimes are used for newly confirmed Word Search words, checked Crossword entries (or automatic correct-word highlighting when enabled), checked Word Pyramid rows, checked Sudoku rows/columns/boxes, and checked Cross Equations equations. Manual-check puzzles do not chime while typing. Repeated checks, restored answers, givens, and reveals are silent; full completion uses the existing celebration instead of layering a progress chime over it. Maze retains its finish sound because it has no confirmed intermediate milestone. `PuzzarySounds.createProgressTracker(readCompleted, isComplete)` provides `check(scope)`, `invalidate()`, and silent `sync()` for modules using checked milestones.

Admin manual creation includes a pixel-painting canvas. Imported or manually entered solution grids must be square JSON arrays of integer 0/1 values, 5 to 15 cells per side. Prints contain clues and starter cells only, not the answer, and support web/PDF/issue-bundle output. The answer is displayed in the admin solution panel.

Regression checks: `PYTHONPATH=. python scripts/verify_nonogram.py`.

## Takuzu / Binairo

Takuzu is a self-contained module without a theme or word-bank dependency. Generate seeded 4x4, 6x6, 8x8, or 10x10 grids. Every row and column must contain equal numbers of 0s and 1s, no three identical consecutive digits, and no duplicate rows or columns. The OR-Tools CP-SAT generator creates a valid solution, then proves the starter cells have exactly one solution by excluding that answer and checking infeasibility. Extra starter cells are added when necessary; timeouts fail generation instead of claiming uniqueness. Difficulty controls the starting-cell target, not a formal human-solving difficulty rating.

Click or tap to cycle blank -> 0 -> 1 -> blank, or choose a 0/1 brush and drag. Keyboard 0/1 sets a digit, Delete/Backspace erases, and arrows navigate. Starter cells are fixed. Progress is account-specific; every cell, including zeros, must be filled to complete. Auto-confirm completed lines defaults to On with row/column highlights and shared progress chimes, and can be switched Off for manual checking. Hint reveals one unfinished or incorrect cell; Reveal Solution fills the board silently. Admins see the solution and can paint a full solution in the manual canvas; the server validates all rules and derives uniquely solvable starter cells. Web/PDF prints and publishing bundles include only starter cells, not the solution.

Regression checks: `PYTHONPATH=. python scripts/verify_takuzu.py` validates all sizes/difficulties using an independent second-solution model, seeds, manual grids, and progress. `scripts/verify_takuzu_tools.py` checks interactive controls and the canvas. `scripts/verify_takuzu_live.py` is opt-in live QA that creates one sample and clears its test progress; `QA_PUZZLE_ID` reuses a sample.

## AI Assistance Disclosure

This project includes AI-assisted scaffolding, implementation, and testing.
