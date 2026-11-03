"""Durable, privacy-safe puzzle activity events."""
from sqlalchemy import desc, select

from app.models import PuzzleActivityEvent


EVENT_LABELS = {
    "started": "Started puzzle",
    "attempt": "Checked an answer",
    "hint": "Used a hint",
    "reveal": "Revealed part of the solution",
    "completion": "Completed puzzle",
    "reset": "Reset puzzle for replay",
}


def event_summary(event: PuzzleActivityEvent) -> str:
    if event.event_type == "attempt":
        return "Correct answer" if (event.event_metadata or {}).get("is_correct") else "Answer checked"
    if event.event_type == "reveal" and (event.event_metadata or {}).get("scope") == "entry":
        return "Revealed one puzzle entry"
    return EVENT_LABELS.get(event.event_type, event.event_type.replace("_", " ").title())


def record_puzzle_event(
    db,
    puzzle_id: str,
    user_id: int | None,
    event_type: str,
    event_metadata: dict | None = None,
    *,
    once_per_run: bool = False,
) -> PuzzleActivityEvent | None:
    if user_id is None or user_id < 0:
        return None
    metadata = event_metadata or {}
    if once_per_run:
        recent = db.scalars(
            select(PuzzleActivityEvent)
            .where(
                PuzzleActivityEvent.puzzle_id == puzzle_id,
                PuzzleActivityEvent.user_id == user_id,
                PuzzleActivityEvent.event_type.in_([event_type, "reset"]),
            )
            .order_by(desc(PuzzleActivityEvent.id))
            .limit(100)
        ).all()
        for event in recent:
            if event.event_type == "reset":
                break
            if event.event_type == event_type and (
                not metadata.get("target")
                or (event.event_metadata or {}).get("target") == metadata.get("target")
            ):
                return None
    event = PuzzleActivityEvent(
        puzzle_id=puzzle_id,
        user_id=user_id,
        event_type=event_type,
        event_metadata=metadata,
    )
    db.add(event)
    return event


def record_progress_transition(db, puzzle, user_id: int | None, plugin, before: dict, after: dict) -> None:
    before_found, before_total = plugin.progress_counter(before or {}, puzzle.solution_payload)
    after_found, after_total = plugin.progress_counter(after or {}, puzzle.solution_payload)
    if after != before:
        record_puzzle_event(db, puzzle.id, user_id, "started", once_per_run=True)
    if after_total > 0 and after_found >= after_total and (before_total <= 0 or before_found < before_total):
        record_puzzle_event(
            db, puzzle.id, user_id, "completion",
            {"found_count": after_found, "expected_count": after_total},
            once_per_run=True,
        )
