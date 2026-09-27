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
- Admin-managed content sources, categories, and tags
- Word-bank content sources for word-game generation
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

## AI Assistance Disclosure

This project was initially scaffolded with AI assistance.
