"""Account-scoped activity shared by the library, account page and admin views."""
from datetime import timezone

from sqlalchemy import case, func, or_, select

from app.models import Puzzle, PuzzleAttempt, PuzzleProgress
from app.permissions import can_view
from app.puzzles import get_puzzle_type
from app.schemas import PuzzleProgressUpdate


def _has_input(value):
    if isinstance(value, dict):
        return any(_has_input(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_input(item) for item in value)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and value is not False


def _changed_from_starters(payload, baseline):
    answers = payload.get('answers', {})
    starter_answers = baseline.get('answers', {})
    changed_answers = isinstance(answers, dict) and any(
        _has_input(value) and value != starter_answers.get(key)
        for key, value in answers.items()
    )
    words = payload.get('found_words', [])
    starter_words = set(baseline.get('found_words', []))
    return changed_answers or (isinstance(words, list) and any(word not in starter_words for word in words))


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def progress_states(db, user, puzzles):
    if not puzzles:
        return {}
    ids = [puzzle.id for puzzle in puzzles]
    saved = {row.puzzle_id: row for row in db.scalars(select(PuzzleProgress).where(
        PuzzleProgress.user_id == user.id, PuzzleProgress.puzzle_id.in_(ids)
    ))}
    attempts = {row.puzzle_id: row for row in db.execute(select(
        PuzzleAttempt.puzzle_id, func.count().label('attempts'),
        func.sum(case((PuzzleAttempt.is_correct.is_(True), 1), else_=0)).label('correct_attempts'),
        func.max(PuzzleAttempt.created_at).label('last_attempt'),
    ).where(PuzzleAttempt.user_id == user.id, PuzzleAttempt.puzzle_id.in_(ids)).group_by(PuzzleAttempt.puzzle_id))}
    states = {}
    for puzzle in puzzles:
        plugin = get_puzzle_type(puzzle.puzzle_type)
        progress = saved.get(puzzle.id)
        payload = (progress.progress_payload or {}) if progress else {}
        found, total = plugin.progress_counter(payload, puzzle.solution_payload) if plugin else (0, 0)
        attempt = attempts.get(puzzle.id)
        attempt_count = attempt.attempts if attempt else 0
        correct_attempts = int(attempt.correct_attempts or 0) if attempt else 0
        baseline = plugin.progress_normalizer(PuzzleProgressUpdate(), puzzle.solution_payload) if plugin and progress else {}
        normalized = plugin.progress_normalizer(PuzzleProgressUpdate.model_validate(payload), puzzle.solution_payload) if plugin and progress else payload
        started = bool(attempt_count or (progress and _changed_from_starters(normalized, baseline)))
        completed = bool(correct_attempts or (started and total > 0 and found >= total))
        if completed:
            found = total
        times = [_utc(value) for value in [progress.updated_at if progress else None, attempt.last_attempt if attempt else None] if value]
        states[puzzle.id] = {
            'found': found, 'total': total,
            'status': 'completed' if completed else 'in_progress' if started else 'not_started',
            'attempts': attempt_count, 'correct_attempts': correct_attempts,
            'last_activity': max(times).isoformat() if times and started else None,
        }
    return states


def account_activity(db, user, viewer=None):
    puzzles = db.scalars(select(Puzzle).where(or_(
        Puzzle.id.in_(select(PuzzleProgress.puzzle_id).where(PuzzleProgress.user_id == user.id)),
        Puzzle.id.in_(select(PuzzleAttempt.puzzle_id).where(PuzzleAttempt.user_id == user.id)),
    ))).all()
    states = progress_states(db, user, puzzles)
    totals = {'played': 0, 'completed': 0, 'in_progress': 0, 'attempts': 0, 'correct_attempts': 0}
    types = {}
    recent = []
    for puzzle in puzzles:
        state = states[puzzle.id]
        if state['status'] == 'not_started':
            continue
        plugin = get_puzzle_type(puzzle.puzzle_type)
        row = types.setdefault(puzzle.puzzle_type, {
            'puzzle_type': puzzle.puzzle_type,
            'label': plugin.label if plugin else puzzle.puzzle_type.replace('_', ' ').title(),
            **dict.fromkeys(totals, 0),
        })
        counts = {'played': 1, 'completed': int(state['status'] == 'completed'),
                  'in_progress': int(state['status'] == 'in_progress'),
                  'attempts': state['attempts'], 'correct_attempts': state['correct_attempts']}
        for key, count in counts.items():
            totals[key] += count
            row[key] += count
        visible = can_view(viewer or user, puzzle, play=True)
        recent.append({
            'id': puzzle.id, 'title': puzzle.title if visible else 'Unavailable puzzle',
            'url': f'/puzzles/{puzzle.id}' if visible else None,
            'label': row['label'], 'archived': puzzle.archived_at is not None, **state,
        })
    recent.sort(key=lambda row: row['last_activity'] or '', reverse=True)
    return {'user_id': user.id, 'totals': totals, 'by_type': sorted(types.values(), key=lambda row: row['label']), 'recent': recent[:20]}
