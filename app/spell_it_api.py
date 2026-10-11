"""Spell It plugin endpoints; hidden answers and hint budgets stay on the server."""
import json
import random
from types import SimpleNamespace
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from app.database import get_db
from app.models import Puzzle, PuzzleAttempt, PuzzleProgress, User
from app.permissions import can_view, register_route_permission, require

router = APIRouter()
register_route_permission('spell_it_preview', 'puzzles.create')


def game():
    from app.puzzle_types import spell_it
    return spell_it


def core():
    from app import main
    return main


async def body(request, limit=64000):
    raw = await request.body()
    if len(raw) > limit: raise HTTPException(413, 'Request too large')
    try: payload = json.loads(raw)
    except (ValueError, RecursionError): raise HTTPException(422, 'Invalid JSON')
    if not isinstance(payload, dict): raise HTTPException(422, 'Expected a JSON object')
    return payload


def context(puzzle_id, request, db):
    user = core().require_api_user(request, db)
    require(user, 'puzzles.play')
    puzzle = db.get(Puzzle, puzzle_id)
    if not puzzle or puzzle.puzzle_type != 'spell_it': raise HTTPException(404, 'Spell It puzzle not found')
    if not core().is_puzzle_type_enabled(db, 'spell_it'): raise HTTPException(400, 'Spell It is disabled')
    if not can_view(user, puzzle, play=True): raise HTTPException(403, 'Puzzle access denied')
    if not getattr(user, 'is_guest_link', False):
        db.scalar(select(User).where(User.id == user.id).with_for_update())
    progress = core().progress_record_for_user(db, puzzle, user, create=True)
    return user, puzzle, progress


def response(puzzle, progress):
    answers = (progress.progress_payload or {}).get('answers', {})
    entries = puzzle.solution_payload['entries']
    return {'answers': answers, 'states': [game().state(entry, answers) for entry in entries], 'letter_banks': [entry['bank'] if entry['settings']['letter_bank'] in ('exact', 'decoy') or answers.get(f"bank:{entry['index']}") == '1' else [] for entry in entries], 'hints_remaining': [max(0, entry['settings']['hint_limit'] - len(game().ledger(answers, entry))) for entry in entries]}


@router.post('/api/spell-it/editor-preview', name='spell_it_preview')
async def editor_preview(request: Request, db=Depends(get_db)):
    core().require_api_user(request, db)
    if not core().is_puzzle_type_enabled(db, 'spell_it'): raise HTTPException(400, 'Spell It is disabled')
    payload = await body(request, 512000)
    try:
        built = game().manual(payload)
    except (ValueError, TypeError) as error: raise HTTPException(422, str(error))
    return {'play_payload': built.play_payload, 'solution_payload': built.solution_payload}


@router.get('/api/puzzles/{puzzle_id}/spell-it/progress')
def read_spell_progress(puzzle_id: str, request: Request, db=Depends(get_db)):
    _, puzzle, progress = context(puzzle_id, request, db)
    # Unsaved baseline is discarded with this read transaction.
    return response(puzzle, progress)


@router.post('/api/puzzles/{puzzle_id}/spell-it/attempts')
async def check_spell_entry(puzzle_id: str, request: Request, db=Depends(get_db)):
    payload = await body(request)
    user, puzzle, progress = context(puzzle_id, request, db)
    answers = payload.get('answers', {})
    if not isinstance(answers, dict): raise HTTPException(422, 'Answers must be an object')
    progress.progress_payload = game().merge_progress(SimpleNamespace(answers=answers), puzzle.solution_payload, progress.progress_payload or {})
    matched, expected = game().count_progress(progress.progress_payload, puzzle.solution_payload)
    if not getattr(user, 'is_guest_link', False):
        db.add(PuzzleAttempt(puzzle_id=puzzle.id, user_id=user.id, answer=json.dumps(progress.progress_payload['answers']), is_correct=matched == expected))
    db.commit()
    return {**response(puzzle, progress), 'completed': matched == expected, 'matched_count': matched, 'expected_count': expected}


@router.post('/api/puzzles/{puzzle_id}/spell-it/hint')
async def spell_hint(puzzle_id: str, request: Request, db=Depends(get_db)):
    payload = await body(request)
    _, puzzle, progress = context(puzzle_id, request, db)
    index = payload.get('entry', 0)
    entries = puzzle.solution_payload['entries']
    if type(index) is not int or not 0 <= index < len(entries): raise HTTPException(422, 'Invalid entry')
    entry = entries[index]
    supplied = payload.get('answers', {})
    if not isinstance(supplied, dict): raise HTTPException(422, 'Answers must be an object')
    normalized = game().merge_progress(SimpleNamespace(answers=supplied), puzzle.solution_payload, progress.progress_payload or {})
    answers = normalized['answers']
    if payload.get('kind') == 'bank':
        if entry['settings']['letter_bank'] != 'hint_triggered': raise HTTPException(400, 'This entry has no hint-triggered bank')
        answers[f'bank:{index}'] = '1'
    else:
        used = game().ledger(answers, entry)
        if len(used) >= entry['settings']['hint_limit']: raise HTTPException(400, 'No letter hints remain')
        positions = [p for p in entry['hidden'] if p not in used and answers.get(f'{index}:{p}') != entry['answer'][p]]
        if not positions: raise HTTPException(400, 'All missing letters are already correct')
        position = random.SystemRandom().choice(positions)
        answers[f'hint:{index}'] = ','.join(map(str, [*used, position]))
        answers[f'{index}:{position}'] = entry['answer'][position]
    progress.progress_payload = game().normalize_progress(SimpleNamespace(answers=answers), puzzle.solution_payload)
    db.commit()
    return response(puzzle, progress)
