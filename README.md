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

## Design Notes

Puzzary is intentionally separate from Home Assistant and Daily Stripe:

- Home Assistant triggers and displays state.
- Daily Stripe renders and prints issues.
- Puzzary generates puzzle content, stores answers, checks attempts, and serves QR pages.

## Puzzle Types

Puzzle types are auto-discovered modules under `app/puzzle_types`.

Each type owns its generator, source adapter, answer checker, and progress logic. Browser UI lives in `app/templates/puzzle_types/{slug}.html`, with optional admin solution display in `app/templates/puzzle_solutions/{slug}.html`.

To add a type, drop a new Python file into `app/puzzle_types/`. Files whose names start with `_` are ignored. The module must call `register_puzzle_type(...)` at import time:

```python
from app.puzzles import GeneratedPuzzle, PrintLayout, PuzzleType, register_puzzle_type


def generate(theme, difficulty, source_data=None, seed=None) -> GeneratedPuzzle:
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
- `GET /api/puzzles/{id}/print-preview` returns the module layout metadata and print/render URLs for Home Assistant, Daily Stripe, or other clients.
- Printed layouts include a QR code by default. The QR points to `/q/{id}`, records a lightweight scan count, and redirects to the matching puzzle page.
- If the user is not logged in after scanning, the login flow preserves the puzzle URL and returns them there after sign-in.
- Set **Admin -> Settings -> Public base URL** so printed QR codes use your public HTTPS address instead of a LAN URL.

If a registered type has no web template, Puzzary falls back to the puzzle prompt and answer submission form. If it has no solution template, admins see the raw solution payload.

`word_entries` is the shared source format Puzzary passes to puzzle modules after applying archive, ban, and excluded-tag filters. Each item is a dict with `word` and `definition` keys; `definition` may be `None`.

Puzzle modules can also support gimmes: pre-solved cells, words, rows, or regions included in the generated payload. A module that supports them should set `supports_gimmes=True`, describe its `gimme_modes`, persist the gimme data in payloads, render it distinctly, and count it as solved in progress/checking logic. The current examples are pre-found Word Search words, pre-filled Crossword entries, and starting Word Pyramid rows.

## AI Assistance Disclosure

This project was initially scaffolded with AI assistance.
