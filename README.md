# Puzzary

Puzzary is a self-hosted, extensible puzzle and game library. Create, manage,
organize, play, and distribute puzzles and games through its web portal or
flexible API.

Puzzle and game types are supplied by modular plugins that own generation,
play, checking, progress, collaboration, and print behavior. External clients
can use the same API for automation and delivery.

## Platform capabilities

- FastAPI app
- PostgreSQL database
- Web portal
- Password-based user accounts
- Admin-managed account creation and password resets
- Searchable puzzle and game library
- Admin panel for archiving, deleting, and organizing puzzles
- Admin-managed dictionary, word banks, categories, and tags
- Canonical dictionary words with reusable properties and tags
- Word banks that reference canonical dictionary words without duplicating them
- Auto-discovered puzzle and game plugin system
- Built-in Word Search, Crossword, Word Pyramid, Sudoku, Nonogram, Maze,
  Cross Equations, Takuzu, and Spell It modules
- Puzzle hints
- Answer checking
- Account-based puzzle progress
- Interactive word-search highlighting
- Photo submission and administrator review workflow
- Flexible authenticated API for external clients and automation
- Printable puzzle payloads and PDFs for publishing integrations

## Dictionary Content Pack

The [English dictionary expansion](content_packs/english-wordnet-5000-v1/README.md)
provides licensed definitions, multiple senses, progressive clues and topic tags
for a library of about 5,000 words. Its opt-in transactional importer preserves
existing content and adds automatic, sense-specific word banks. Source data has
its own bundled licenses; it is not part of the application's code license.

## Run

Prerequisites are Docker Engine and Docker Compose v2. The full
[local Docker deployment guide](docs/docker-deployment.md) covers persistent
data, upgrades, automatic schema migrations, backups, restores, and health
checks.

Copy the sample environment:

```bash
cp .env.example .env
```

Replace every `change-*` value in `.env` before exposing Puzzary outside your
local machine. Keep `.env` out of version control.

Start the stack:

```bash
docker compose up -d --build
```

Confirm both services are running and the application is healthy:

```bash
docker compose ps
curl --fail http://localhost:8790/health
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

Admins can issue per-account API tokens from the Users page. Tokens are shown once, then stored only as hashes. Use **Rotate API Token** on that account's Security tab to replace the credential; rotation immediately invalidates the previous token.

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
  --data '{"puzzle_type":"word_search","theme":"general","difficulty":"easy"}'
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

## Architecture and integrations

Puzzary is the system of record for puzzle and game content, solutions,
attempts, progress, and printable output. Its web portal is a complete client,
and the API supports additional clients and delivery workflows:

- Puzzle modules extend the platform without changing the core application.
- Home Assistant can trigger workflows and display state through the API.
- Daily Stripe can render and print Puzzary issues as a supported publishing
  integration.
- Other clients can generate, import, export, play, and print content through
  the same authenticated API.

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
  "title": "Community Puzzle Pack",
  "description": "This week's puzzles",
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

## Puzzle visibility

Published puzzles may be **Public** or **Unlisted**. Public puzzles appear in the library, filters, search, latest-puzzle API, publishing choices and eligible public Group Solve sessions. Unlisted puzzles are omitted from every discovery surface but remain available to signed-in users who have the direct puzzle URL and normal puzzle-view permission. Their QR image and short QR redirect also require an authorized signed-in session and use private caching.

Unlisted is independent of draft, archive and access permissions: drafts are unpublished previews for authorized editors, archived puzzles use the archive permissions, and unlisted published puzzles are playable through a direct link. Administrators and authorized publishers can still find, edit, export and change unlisted puzzles on the puzzle-management page. Anonymous Group Solve guests cannot enter an unlisted puzzle merely through a group invitation, and unlisted puzzles cannot be added to a published issue. Portable exports retain the visibility metadata, as do imports.

Regression check: `PYTHONPATH=. python scripts/verify_unlisted_puzzles.py`.

## Puzzle access passwords

Publishers and managers can add, rotate or remove an optional password on each puzzle. Puzzle passwords use the same PBKDF2 policy as account passwords but are separate credentials: at least 10 characters with uppercase, lowercase and a number. Only the hash is stored. An unlocked browser session is bound to the current hash, so rotating the password immediately relocks existing sessions; removing it opens the puzzle normally. Repeated incorrect attempts are rate limited.

Locked library cards show a lock indicator and a generic preview without clues. Direct web access presents an unlock form before puzzle, hint, print, solution or progress data is loaded. API clients pass `X-Puzzle-Password`; all puzzle detail, progress, action and print endpoints enforce the same gate. Latest-puzzle feeds skip locked entries, portable exports never contain the hash, unrelated exporters cannot include locked content, and password-protected puzzles must be unlocked or have their password removed before use on a public publishing surface. Print-render tokens are bound to the current password revision.

Group Solve session passwords and puzzle passwords are independent. Anonymous guests must supply both when both are enabled; neither credential is put in the invite URL. The public lobby discloses only that each gate exists. Moving a Group Solve to a protected puzzle requires that the host first unlock it. Regression check: `PYTHONPATH=. python scripts/verify_puzzle_passwords.py`.

## Expiring guest links

Authorized puzzle managers can create revocable, unguessable guest links from the puzzle-management page. Links may have an expiration, total-use cap and active-guest cap; the plaintext URL is displayed only when it is created, while the database stores only its SHA-256 hash. Draft and archived puzzles are ineligible. Published unlisted and password-protected puzzles are eligible, and a protected puzzle still requires its separate puzzle password.

Each activation creates a browser-scoped guest identity with persisted puzzle progress. Guest links grant play only: they do not grant library discovery, printing, exports, account access, photo uploads, admin access or solution access. A manager may opt a link into Group Solve; that opt-in permits its guest to join an allow-guests, passwordless session for the same puzzle, including an unlisted puzzle, while expiry and revocation continue to be checked on every request. Regression check: `PYTHONPATH=. python scripts/verify_guest_links.py`.

## Puzzle series

Publishers with puzzle-metadata permission can assemble active published puzzles into an ordered series under **Admin > Series**. Prerequisite enforcement is off by default; when enabled, every earlier active stage must be complete before a later stage opens. The puzzle page shows the full order, current step, per-stage progress, unavailable entries and locks, and an optional after-stage message. The same lock is enforced for direct web links, progress/actions, print endpoints and puzzle APIs. Series owners and users with metadata permission for all puzzles can bypass locks for administration.

Archived, auto-archived and draft puzzles remain visible as unavailable series entries but are skipped when prerequisites are evaluated, so an editorial change cannot deadlock every later stage. Removing or reordering entries immediately recomputes access; archiving the series disables its navigation and locks without changing its puzzles. Group Solve can use shared progression, where the group’s saved state unlocks its next stage for everyone, or individual progression, where each signed-in participant needs their own prior completion. Puzzle-specific guest links never inherit progress or access to another stage. Regression check: `PYTHONPATH=. python scripts/verify_puzzle_series.py`.

## Group Solve

Hosts can use **Session settings > Move group to another puzzle** to search active, collaboration-compatible puzzles. Moving keeps the same group, participants, guest permissions and invite link (`/groups/{id}`), and connected players follow automatically. Each puzzle's shared progress is saved when leaving and restored when returning. Delayed edits from an earlier puzzle are rejected rather than applied to the new board. Groups support up to 100 visited puzzles.

API hosts select puzzles with `GET /api/groups/{id}/puzzles?q=...&offset=0`, then `POST /api/groups/{id}/puzzle` with `puzzle_id` and the current `revision`. After the first move, action requests must include the current `puzzle_id` as well as `revision`; old-board or pre-switch requests return 409.

Committed puzzle edits, checks, hints, reveals and session changes are pushed over the live connection as authorized per-player snapshots. Joining/reconnecting receives the current snapshot immediately. Group play flushes local changes in bounded 40 ms batches instead of the normal single-player debounce; continuous typing/painting does not postpone synchronization until the player stops. The existing HTTP action API, PostgreSQL row locks, idempotency and cell revision conflict checks remain authoritative. A busy/dirty browser queues the newest pushed snapshot until its local edit is acknowledged, protecting unsent input and preserving same-cell conflict detection. Live connections use a ten-second reconciliation read; HTTP-only clients retain one-second polling. Cursor packets cannot alter puzzle answers.

Cursor presence uses an authenticated same-origin WebSocket with about 30 position updates per second and animation-frame smoothing (roughly 22 ms blending, no long CSS transition). The relay coalesces obsolete positions, bounds message size/rate, checks account/session/membership and session closure at least every two seconds, and keeps cursor traffic separate from puzzle revisions. Browsers reconnect automatically and fall back to HTTP presence when WebSockets are unavailable. HTTP updates are relayed to connected WebSocket players too. High-frequency positions are transient; only a low-frequency presence heartbeat touches the database. Use the default single Uvicorn worker for the in-process live relay; multi-worker deployments need a shared broker for real-time cross-worker presence. HTTPS deployments use `wss:`; reverse proxies must forward WebSocket upgrades. Reduced-motion users get immediate positions without interpolation.

Named, color-coded mouse pointers, active cells and text carets appear as non-interactive overlays inside the puzzle area. Crossword also shows the active entry. Touch players share their selected cell instead of a hovering pointer. Pointer coordinates are normalized for different screen sizes; idle pointers fade after four seconds. Presence updates are separate from answer revisions and never transmit typed values. Hidden/background pages clear their indicators, and stale presence disappears after four seconds without updates. Overlays are excluded from print and respect reduced motion.

From any built-in puzzle, expand Group Solve to start a fresh shared session or enter a session code. Invite links open the same puzzle and shared progress; personal answers, attempts and photos remain separate. Participants sign in by default (API clients use their own account token). An admin hosting a group can enable **Allow guests** when creating it or in **Session settings**. Invitees then choose a guest display name without creating an account. Their signed session is scoped to that group; approval, limits, removal and realtime collaboration still apply. Disabling guests revokes guest HTTP/WebSocket access, and guests never gain account, library or admin access.

Hosts can optionally protect anonymous guest entry with a session password. Passwords use the account-password policy and are stored only as PBKDF2 hashes; snapshots expose only `password_protected`. A missing and an incorrect password return the same error, repeated failures are throttled per session and client address, and plaintext passwords are never placed in invite URLs. Changing or removing the password takes effect for new guest joins without disconnecting guests who already hold a valid group-scoped session. A removed guest still cannot rejoin. Approval-required sessions apply approval after the password gate. Signed-in account joins keep their existing authorization flow; when access-code and room-lock features are added, those gates must compose before membership is created.

The public lobby at `/groups` lists only open sessions whose host enables **List in Public Group Solve Lobby**. Listing is off by default and can be changed without ending the session. A host change delists the session until the new host explicitly opts in. Public cards contain only the puzzle title/type, host display name, active player count/capacity, join status, guest availability, guest-password status and whether a separate puzzle password is required; session codes, passwords, shared progress and hidden puzzle content are excluded. Draft, archived, hidden, unlisted and private puzzles never appear. Full sessions remain visible as full, in-progress sessions permit late joining while capacity remains, and closed or delisted sessions disappear on the next 15-second refresh. Lobby pages and API responses are not cached, and discovery refreshes are rate limited.

Supported adapters: Crossword, Sudoku, Cross Equations, Nonogram, Takuzu, Word Search, Word Pyramid, Spell It and Maze. Word Search shares unchecked and incorrect selections as well as confirmed finds. Pyramid shares row answers, incremental clues and row reveals; Spell It shares letter edits, checks and server-enforced hint budgets.

The host can transfer control to any connected, eligible signed-in participant from the participant list. Eligible successors need both **Host groups** and **Moderate own groups** permissions. A voluntary transfer keeps the former host in the session. If the host explicitly leaves, the earliest-joined eligible active participant becomes host immediately, even if their browser is briefly away. If the host disconnects unexpectedly, the earliest connected successor is promoted after `GROUP_HOST_RECONNECT_GRACE_SECONDS` (30 seconds by default). Guests are never promoted. If no eligible host remains, the session closes while preserving its shared state. Host changes use the existing realtime channel, and the former host rejoins only as a regular participant.

Maze hosts choose **Co-op**, **Companions**, or **Rally** at creation or in Session settings. Co-op uses one shared path. Companions gives every active player a separate, named/color-coded path; the group finishes when all active players reach the exit. Rally places numbered checkpoints on a valid route and opens one connecting leg at a time. Legs rotate through the active member list in join order, including guests. A member leaving or being removed hands uncompleted legs to the remaining roster; completed legs remain intact. Choose enough Rally legs for everyone to have a turn (2-50, capped by route length). Changing mode or leg count clears maze paths; unrelated session settings preserve them. Modes and paths are retained when the host moves to another puzzle and returns. Group mazes require valid wall-respecting paths; Mark Complete is only available in solo play.

The create/settings API accepts `puzzle_options: {maze_mode: "rally", rally_legs: "4"}` (mode values: `co_op`, `companions`, `rally`). Maze patch keys are `path`, `path:<participant_id>`, or `leg:<zero_based_index>` respectively, with JSON-encoded coordinate lists as values. Participant IDs and their active order appear in the snapshot's `context.participants`. The server, not the caller, supplies the actor identity and enforces ownership/turn order. Checkpoints expose only endpoints, not the connecting solution. `hint` extends the active path by one legal cell toward its destination; `reveal` finishes that path/leg. Backtrack from a dead end before requesting a continuation hint.

Sessions persist in PostgreSQL with a seven-day expiry. The host remains the host when offline or leaving, and can rejoin without losing state. A limit of 0 uses the server cap of 50 members, including the host; each account may host up to 20 open sessions. Hosts can choose invite-link access or require approval, approve/remove players, change the limit, and close a session. Removed players cannot rejoin. Closed/expired sessions preserve their state for existing participants but reject changes and new joins.

The browser polls authoritative snapshots every second without refreshing the page. Presence expires after 20 seconds; focused cells and recent contributions are highlighted and attributed. API edits are incremental, idempotent actions serialized with a PostgreSQL row lock. Revisions track changes per cell: stale edits to disjoint cells merge; stale edits to a changed cell return 409 instead of overwriting it. A rejected browser edit restores current shared state and shows a conflict message. Offline unsent changes are kept for reconnect; a later conflict still requires re-entry. Joining/reloading does not replay the completion sound.

API (session-cookie or bearer-token authenticated unless noted):
- `POST /api/puzzles/{id}/groups`: `{participant_limit: 0, approval_required: false, allow_guests: true, password: "optional guest password", public_lobby: false}`.
- `POST /api/groups/join`: `{code: "SESSIONCODE"}` (also accepts the group ID); approval requests return `pending: true` without revealing state.
- `POST /api/groups/{id}/guests`: `{name: "Guest", password: "..."}`; the password is required only when the snapshot advertises `password_protected: true`.
- `GET /api/groups/lobby?q=...&puzzle_type=...&available=open&password=required&sort=recent`: public, privacy-filtered session discovery. Sort values are `recent`, `newest` and `players`.
- `GET /api/groups/{id}?focus=0,1`: snapshot, revision, shared state/counts, members/presence and recent action attribution.
- `POST /api/groups/{id}/actions`: `{action_id: "UUID", revision: 0, kind: "patch", changes: {"0,1": "1"}}`; an empty value erases. `check`, `hint` and `reveal` are shared actions. Crossword also supports `clue`, `check_entry`, `reveal_entry` with `target: "1:across"`.
- `POST /api/groups/{id}/members`: `{user_id: 2, action: "approve"}` or `remove` (host only).
- `POST /api/groups/{id}/settings`: same options as create (host only). An empty `password` keeps the current password; send a replacement password to rotate it or `remove_password: true` to remove it. `public_lobby` controls public discovery independently of join authorization.
- `POST /api/groups/{id}/presence`: `{pointer: {key: "0,1", x: 0.5, y: 0.5}, focus: "0,1", caret: 0, selection_end: 1, entry: ["0,1"]}`. Coordinates range from 0 to 1 relative to the named cell, or the puzzle panel when `key` is empty. Cell anchoring keeps pointers aligned across desktop/mobile layouts. Returns members only; does not modify answers or their revision. Omit fields to clear presence. Requires active membership in an open session.
- `WS /api/groups/{id}/presence/ws`: send the same presence payload. Receive `ready` (initial members) and `presence` (changed members) events. Browser clients authenticate with their session and must have a matching Origin; native clients can use an Authorization bearer header. Tokens must not be put in URLs. Invalid/oversized/excessive messages or revoked membership close the connection.
- `POST /api/groups/{id}/leave` and `/close` (close is host only).

Plugin contract: opt in with `PuzzleType.collaboration(state, action, solution) -> state`. The hook validates/applies edits; the existing progress counter determines shared completion. `app.group_actions.cell_collaboration(normalize_progress)` supplies cell/entry actions for coordinate-based plugins. The shared layer records answer-key ownership, typed actions and timestamps without copying answer values into attribution records; it also recognizes found words, completed entries and progressive Sudoku units. A plugin with additional public contribution concepts can return safe event dictionaries from `PuzzleType.collaboration_attribution(before, after, action)`. Web modules register `window.PuzzaryGroup.register({read, apply})` and route saves to `PuzzaryGroup.save()` rather than personal progress. The API contract advertises `supports_group_solve`; unadapted plugins remain unavailable. Server state never contains the full solution, and fixed givens cannot be edited. Test with `PYTHONPATH=. python scripts/verify_groups.py` inside the application runtime; `verify_group_passwords.py` covers password security and guest lifecycle, and `verify_group_lobby.py` covers public discovery. All use isolated temporary SQLite databases, never the live database.

Appearance themes use validated semantic tokens rather than arbitrary CSS. Puzzary includes Light, Dark, and Follow System modes, per-user preferences, an administrator-selected guest/server default, and portable JSON theme packages. See [docs/appearance-themes.md](docs/appearance-themes.md) and the example package in `content_packs/midnight-garden.puzzary-theme.json`. Run `PYTHONPATH=. python scripts/verify_themes.py` for isolated package, permission, browser, contrast, and fallback checks.

Optional plugin hooks: `collaboration_fields` declares host controls using `GenerationField`; `collaboration_setup(state, options, solution)` initializes or changes mode state without a schema migration. `collaboration_value_limit` bounds each patch value (16 by default; Maze allows serialized paths). Actions receive authenticated `_actor` and active `participants` from the server. Frontend `apply(state, snapshot)` receives participant context; `scope(answers, snapshot)` limits diffing to a player's editable values. Use `PuzzaryGroup.action(kind, target)` for module-specific checks/hints/clues/reveals and `PuzzaryGroup.color(participantId)` for the participant's stable, session-unique color. Names, numbered markers, tooltips and the contribution summary keep attribution accessible without relying on color alone, including after a contributor leaves. Plugin authors must provide both server and browser adapters; registration never silently bypasses game validation. `PYTHONPATH=. python scripts/verify_all_group_puzzles.py` checks all built-ins plus typed attribution, ten-player color uniqueness, ownership transfer/clears/departures, two-browser play, all maze modes, invalid paths/ownership, hint budgets, reload, responsive views and personal-progress isolation in a disposable database.

Rally shows an ordered list of leg assignments and checkpoint pairs, with the current player highlighted both in that list and in the member roster. The next leg is labeled Up next; later legs wait. A confirmed non-final leg plays the shared progress chime once; the final leg uses the puzzle completion sound. Initial loads, reconnects without a new completion, and leg-count resets do not replay old chimes. Turn ownership follows authoritative snapshots, not optimistic local paths. Snapshots include `puzzle_epoch`, which changes when switching puzzles or resetting a game mode; clients discard obsolete pending edits on a new epoch without requiring a refresh.

## Completed-work photo submissions

Signed-in players can upload completed paper work from a puzzle page and optionally associate it with one of their digital attempts for the same puzzle. Puzzary accepts JPEG, PNG and WebP images up to 10 MB and identifies the format from the file contents rather than trusting the filename or browser media type. Notes are limited to 2,000 characters. Files are stored under `UPLOAD_DIR` (by default `/data/uploads`) and are served only through authenticated, permission-checked routes; they are not public static assets.

Players can preview and delete their own submissions. Accounts with `activity.view` can review the image, puzzle, submitting account, notes and upload time under **Admin > Activity**, and can delete a submission. Archiving a puzzle preserves its submission metadata and files for authorized archive/activity review. Permanently deleting a photo or its puzzle removes both the database record and the stored file. Regression check: `PYTHONPATH=. python scripts/verify_photo_submissions.py` uses an isolated SQLite database and temporary upload directory.

## Account Activity and Library Progress

**Account** shows personal completion and attempt totals, a breakdown by puzzle type, and recent recorded activity. Administrators with `activity.view` can see the same summary under **Users > Activity**. `GET /api/account/stats` returns the authenticated account's totals, `by_type` rows and recent activity; bearer requests require `api.use` and library access. Supplying another user ID does not change the account being summarized.

Library states are account-specific: Not started, In progress, and Completed. Module normalizers distinguish player input from starter cells and gimme words; opening a puzzle or saving only its starters does not count as playing it. Correct submitted attempts also mark completion. Creating or administering a puzzle does not create personal solve progress, and Group Solve progress remains separate.

Archived puzzles remain excluded from ordinary libraries; accounts explicitly granted archive access can use the Archived availability filter. Personal statistics retain recorded activity on archived puzzles, but cannot reveal inaccessible titles or links. Resetting a personal puzzle removes that account's progress and attempts, so statistics reflect retained records rather than a permanent lifetime ledger. Regression checks: `PYTHONPATH=. python scripts/verify_user_activity.py` uses an isolated SQLite database; `ACTIVITY_QA_DATABASE` must only point to a dedicated disposable PostgreSQL database, never production.

## Roles and Permissions

Open **Admin > Users** for a searchable, status-filtered account grid. The add button opens account creation, and each account has Profile, Access, Security and Activity tabs. Access includes assigned roles, searchable grouped overrides and an effective-permission preview; filtered or hidden fields remain part of the save. Security actions require confirmation and preserve one-time password setup links and API token delivery. Activity shows recorded logins, API use, recent puzzle progress and authorized audit events, not inferred connected devices. **Admin > Roles** creates, duplicates and edits roles; **Audit Log** records permission changes and account security actions with actor, timestamp and before/after values. Role changes affect every assigned account. User and role controls show high-privilege warnings.

Default editable presets:
- **Player:** library, personal play/progress, print, dictionary reads, joining/inviting groups, API access.
- **Publisher:** Player plus generation, manual creation, import/export, own drafts and own puzzle/issue management, hosting/moderating groups.
- **Manager:** Publisher plus management of any puzzle/issue, archived/draft access, dictionary/word-bank/tag management, activity review and moderation of any group.
- **Admin:** currently registered capabilities, including users, roles, server settings, plugins, solutions, guests and audit access. It is not a wildcard grant for future plugins.

Multiple roles are merged by union. Per capability: **explicit user deny > explicit user allow > role grants > default deny**. Reset to Role Defaults clears overrides without replacing assigned roles. Unknown permissions never authorize a request. `puzzles.<action>.own` requires `created_by_id` to match the account; `.any` also covers owned resources. Own and any are independent permissions: to block an action completely, deny both scopes. Edit, publish, archive, permanent delete, scheduling and metadata are separately grantable. There is no soft-delete/restore workflow beyond archiving; permanent deletion requires confirmation and removes associated progress.

Generating/importing without publishing permission creates an unpublished draft. Give that account `puzzles.drafts.own` to preview it. Manual creation/editing cannot publish without the relevant publishing permission. Setting custom auto-archive dates requires scheduling access. Viewing archived puzzles and playing them are independent toggles. Editors necessarily see the payloads for resources they can edit; the solution sidebar for players uses a separate permission.

User creation, profile editing, account status, password resets, session revocation and token rotation are independently restricted. Assigning roles or overrides requires `roles.manage`; users without it cannot grant Admin during creation or through the legacy profile form. Modifying another role administrator's profile/security requires role management too. Permission writes are serialized and cannot remove the last active account with role, user and server management capabilities. Bootstrap does not re-promote an account after its permissions have been initialized.

Web/API authorization uses the same catalogue and ownership checks. Bearer-token requests additionally require `api.use`. Session cookies do not freeze permissions: every request loads current authority, and Group Solve sockets recheck it within two seconds. Guests remain scoped to their opted-in group; enabling guests requires `groups.guests` for the actor and host. External print renderers should fetch `render_url` from authenticated `GET /api/puzzles/{id}/print-preview`. That URL is puzzle-specific, expires after 15 minutes, and rechecks account/session/library/print permissions at render time. It does not grant general API access. Direct render requests require an authenticated cookie/token.

Plugins may call `register_permission('my_plugin.action', 'Action label')` and `register_route_permission('endpoint_name', 'my_plugin.action')` from `app.permissions`, then use the shared authentication dependency in that endpoint. New plugin capabilities are denied until explicitly assigned to a role/account; they do not automatically expand existing Admin roles. The common progress/group APIs also enforce play/group permissions before invoking game hooks. Registered endpoint names must be unique. SSO mappings and optional per-collection/tag/type restrictions are future extensions, not implemented here; any future mapped roles must pass through the same local override resolution.

Migration preserves existing administrators and existing player generation/hosting capabilities in an **Existing Player** role, without adding content-management or user-management grants. New accounts start with Player. Existing puzzles, progress and account tokens are retained. Tests: `PYTHONPATH=. python scripts/verify_permissions.py` runs against isolated SQLite; set `PERMISSIONS_QA_DATABASE` to a **dedicated, disposable PostgreSQL database** for concurrent last-admin tests. The script drops its test schema; never point it at production.

## AI Assistance Disclosure

This project includes AI-assisted scaffolding, implementation, and testing.
## Word-bank transfers

Word Banks includes an import wizard for UTF-8 TXT, CSV and JSON, pasted data,
raw URLs, GitHub/Gists, Pastebin and other Puzzary servers. Preview field mapping,
errors and duplicates before importing into a new or existing bank. Export from
a bank's page to transfer its definitions and attribution.
See [word-bank format, API and security limits](docs/word-bank-imports.md).
