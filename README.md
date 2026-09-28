# Puzzary

Puzzary is a self-hosted puzzle library and answer portal for Daily Stripe.

It owns puzzle generation, stored solutions, hints, attempts, photo submissions, and QR-backed web pages. Home Assistant and Daily Stripe should call Puzzary through its API instead of storing puzzle state themselves.

## Current MVP

- FastAPI app
- PostgreSQL database
- Web portal
- Household user profiles
- Household profile login
- Puzzle library view
- Admin panel for archiving, deleting, and organizing puzzles
- Admin-managed dictionary, word banks, categories, and tags
- Canonical dictionary words with reusable properties and tags
- Word banks that reference canonical dictionary words without duplicating them
- Word-search puzzle generator
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

Admin panel:

```text
http://localhost:8790/admin
```

## API

Generate a puzzle:

```bash
curl -X POST http://localhost:8790/api/puzzles/generate \
  -H "Content-Type: application/json" \
  --data '{"puzzle_type":"word_search","theme":"daily_stripe","difficulty":"easy"}'
```

Get a puzzle:

```bash
curl http://localhost:8790/api/puzzles/{puzzle_id}
```

Submit an answer:

```bash
curl -X POST http://localhost:8790/api/puzzles/{puzzle_id}/attempts \
  -H "Content-Type: application/json" \
  --data '{"user_id":1,"answer":"MERIDIAN,PUZZLE,ZEBRA,PRINT,CLUE"}'
```

Save logged-in word-search progress:

```bash
curl -X POST http://localhost:8790/api/puzzles/{puzzle_id}/progress \
  -H "Content-Type: application/json" \
  --data '{"found_words":["MERIDIAN","PUZZLE"]}'
```

## Design Notes

Puzzary is intentionally separate from Home Assistant and Daily Stripe:

- Home Assistant triggers and displays state.
- Daily Stripe renders and prints issues.
- Puzzary generates puzzle content, stores answers, checks attempts, and serves QR pages.

## Puzzle Types

Puzzle types are registered modules under `app/puzzle_types`.

Each type owns its generator, word-bank adapter, answer checker, and progress logic. Browser UI lives in `app/templates/puzzle_types/{slug}.html`, with optional admin solution display in `app/templates/puzzle_solutions/{slug}.html`.

To add a type, create a module that calls `register_puzzle_type(...)`, then import it from `load_builtin_puzzle_types()` in `app/puzzles.py`.

## AI Assistance Disclosure

This project was initially scaffolded with AI assistance.
