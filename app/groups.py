"""Persistent, account-only Group Solve sessions. Row locks serialize all writes."""
from datetime import datetime, timedelta, timezone
from typing import Literal
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import GroupSolve, GroupMember, GroupAction, Puzzle, User
from app.puzzles import get_puzzle_type

router = APIRouter()
SERVER_LIMIT = 50


def core():
    from app import main
    return main


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class GroupOptions(BaseModel):
    participant_limit: int = Field(default=0, ge=0, le=SERVER_LIMIT)
    approval_required: bool = False


class Join(BaseModel):
    code: str = Field(min_length=1, max_length=26)


class Action(BaseModel):
    action_id: uuid.UUID
    revision: int = Field(ge=0)
    kind: Literal['patch', 'check', 'hint', 'reveal', 'clue', 'reveal_entry', 'check_entry'] = 'patch'
    target: str = Field(default='', max_length=80)
    changes: dict[str, str] = Field(default_factory=dict, max_length=4096)


class MemberControl(BaseModel):
    user_id: int
    action: Literal['approve', 'remove']


def group_for(db, group_id, lock=False):
    query = select(GroupSolve).where(GroupSolve.id == group_id)
    group = db.scalar(query.with_for_update() if lock else query)
    if not group: raise HTTPException(404, 'Group Solve not found')
    return group


def member_for(db, group, user, active=True):
    member = db.scalar(select(GroupMember).where(GroupMember.group_id == group.id, GroupMember.user_id == user.id))
    if not member or (active and member.status != 'active'):
        raise HTTPException(403, 'Join this Group Solve first; approval may be required')
    return member


def writable(group):
    if group.closed or utc(group.expires_at) <= now():
        raise HTTPException(409, 'This Group Solve is closed or expired')


def snapshot(db, group, user):
    puzzle = db.get(Puzzle, group.puzzle_id)
    plugin = get_puzzle_type(puzzle.puzzle_type)
    if not plugin or not plugin.collaboration: raise HTTPException(410, 'Collaboration plugin is unavailable')
    found, total = plugin.progress_counter(group.state, puzzle.solution_payload)
    members = db.execute(select(GroupMember, User).join(User, GroupMember.user_id == User.id)
                         .where(GroupMember.group_id == group.id)).all()
    recent = db.scalars(select(GroupAction).where(GroupAction.group_id == group.id)
                        .order_by(GroupAction.id.desc()).limit(20)).all()
    names = {m.user_id: u.display_name for m, u in members}
    return {'id': group.id, 'code': group.code, 'puzzle_id': group.puzzle_id,
            'host_id': group.host_id, 'user_id': user.id, 'revision': group.revision,
            'state': group.state, 'found_count': found, 'expected_count': total,
            'completed': total > 0 and found == total,
            'closed': group.closed or utc(group.expires_at) <= now(),
            'expires_at': group.expires_at.isoformat(),
            'participant_limit': group.participant_limit, 'server_limit': SERVER_LIMIT,
            'approval_required': group.approval_required,
            'url': f'/puzzles/{group.puzzle_id}?group={group.id}',
            'members': [{'user_id': m.user_id, 'name': u.display_name, 'status': m.status,
                         'online': bool(m.seen_at and (now()-utc(m.seen_at)).total_seconds() < 20),
                         'focus': m.focus if m.seen_at and (now()-utc(m.seen_at)).total_seconds() < 20 else ''}
                        for m, u in members if m.status == 'active' or user.id == group.host_id],
            'recent': [{'revision': a.revision, 'user_id': a.user_id, 'name': names.get(a.user_id, 'Player'),
                        'kind': a.kind, 'keys': list(a.changes)} for a in recent]}


@router.post('/api/puzzles/{puzzle_id}/groups')
def create_group(puzzle_id: str, payload: GroupOptions, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if not puzzle: raise HTTPException(404, 'Puzzle not found')
    plugin = get_puzzle_type(puzzle.puzzle_type)
    if not plugin or not plugin.collaboration: raise HTTPException(400, 'This plugin does not support Group Solve')
    count = db.scalar(select(func.count()).select_from(GroupSolve).where(
        GroupSolve.host_id == user.id, GroupSolve.closed == False, GroupSolve.expires_at > now()))
    if count >= 20: raise HTTPException(429, 'Close an existing Group Solve before creating another')
    group = GroupSolve(id=uuid.uuid4().hex[:24], code=secrets.token_hex(5).upper(),
                       puzzle_id=puzzle.id, host_id=user.id, state={'answers': {}}, cell_versions={},
                       expires_at=now()+timedelta(days=7), **payload.model_dump())
    db.add(group); db.flush()
    db.add(GroupMember(group_id=group.id, user_id=user.id, status='active', seen_at=now()))
    db.commit()
    return snapshot(db, group, user)


@router.post('/api/groups/join')
def join_group(payload: Join, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    code = payload.code.strip()
    group = db.scalar(select(GroupSolve).where((GroupSolve.code == code.upper()) | (GroupSolve.id == code)).with_for_update())
    if not group: raise HTTPException(404, 'Group Solve not found')
    member = db.scalar(select(GroupMember).where(GroupMember.group_id == group.id, GroupMember.user_id == user.id))
    if member and member.status == 'removed': raise HTTPException(403, 'You were removed from this session')
    if member and member.status == 'active': return snapshot(db, group, user)
    writable(group)
    active = db.scalar(select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group.id, GroupMember.status == 'active'))
    if active >= (group.participant_limit or SERVER_LIMIT): raise HTTPException(409, 'Participant limit reached')
    if not member:
        member = GroupMember(group_id=group.id, user_id=user.id)
        db.add(member)
    member.status = 'pending' if group.approval_required and user.id != group.host_id else 'active'
    member.seen_at = now()
    db.commit()
    return {'pending': True, 'url': f'/puzzles/{group.puzzle_id}?group={group.id}', 'id': group.id} if member.status == 'pending' else snapshot(db, group, user)


@router.get('/api/groups/{group_id}')
def read_group(group_id: str, request: Request, focus: str = '', db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id)
    member = member_for(db, group, user)
    member.seen_at = now(); member.focus = focus[:80]
    db.commit()
    return snapshot(db, group, user)


@router.post('/api/groups/{group_id}/actions')
def group_action(group_id: str, payload: Action, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True)
    member_for(db, group, user); writable(group)
    duplicate = db.scalar(select(GroupAction).where(GroupAction.group_id == group.id, GroupAction.action_id == str(payload.action_id)))
    if duplicate:
        if duplicate.user_id != user.id: raise HTTPException(409, 'Action ID already used')
        return snapshot(db, group, user)
    if payload.revision > group.revision: raise HTTPException(409, 'Invalid future revision')
    if payload.kind != 'patch' and payload.revision != group.revision:
        raise HTTPException(409, 'Group changed; retry after synchronizing')
    conflicts = [k for k in payload.changes if group.cell_versions.get(k, 0) > payload.revision]
    if conflicts: raise HTTPException(409, {'message': 'Another player changed these cells; your edit was not applied', 'keys': conflicts})
    if any(len(k) > 80 or len(v) > 16 for k, v in payload.changes.items()): raise HTTPException(422, 'Cell value too long')
    puzzle = db.get(Puzzle, group.puzzle_id)
    plugin = get_puzzle_type(puzzle.puzzle_type)
    if not plugin or not plugin.collaboration: raise HTTPException(400, 'Plugin no longer supports Group Solve')
    try: state = plugin.collaboration(group.state, payload.model_dump(mode='json'), puzzle.solution_payload)
    except ValueError as error: raise HTTPException(422, str(error))
    before = group.state.get('answers', {})
    after = state.get('answers', {})
    changed = {k: after.get(k, '') for k in set(before) | set(after) if before.get(k, '') != after.get(k, '')}
    group.revision += 1
    group.state = state
    group.cell_versions = {**group.cell_versions, **{k: group.revision for k in changed}}
    db.add(GroupAction(group_id=group.id, action_id=str(payload.action_id), user_id=user.id,
                       revision=group.revision, kind=payload.kind, changes=changed))
    db.commit()
    return snapshot(db, group, user)


@router.post('/api/groups/{group_id}/members')
def manage_member(group_id: str, payload: MemberControl, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True); writable(group)
    if user.id != group.host_id: raise HTTPException(403, 'Only the host can manage participants')
    if payload.user_id == group.host_id: raise HTTPException(400, 'The host cannot be removed')
    member = db.scalar(select(GroupMember).where(GroupMember.group_id == group.id, GroupMember.user_id == payload.user_id))
    if not member: raise HTTPException(404, 'Participant not found')
    if payload.action == 'approve':
        active = db.scalar(select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group.id, GroupMember.status == 'active'))
        if member.status != 'pending': raise HTTPException(400, 'No pending request')
        if active >= (group.participant_limit or SERVER_LIMIT): raise HTTPException(409, 'Participant limit reached')
        member.status = 'active'
    else: member.status = 'removed'
    db.commit()
    return snapshot(db, group, user)


@router.post('/api/groups/{group_id}/leave')
def leave_group(group_id: str, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True)
    member = member_for(db, group, user, active=False)
    if member.status != 'removed' and user.id != group.host_id: member.status = 'left'
    member.focus = ''; member.seen_at = None
    db.commit()
    return {'left': True}


@router.post('/api/groups/{group_id}/close')
def close_group(group_id: str, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True)
    if user.id != group.host_id: raise HTTPException(403, 'Only the host can close this session')
    group.closed = True; db.commit()
    return snapshot(db, group, user)


@router.post('/api/groups/{group_id}/settings')
def group_settings(group_id: str, payload: GroupOptions, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True); writable(group)
    if user.id != group.host_id: raise HTTPException(403, 'Only the host can change settings')
    active = db.scalar(select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group.id, GroupMember.status == 'active'))
    if payload.participant_limit and payload.participant_limit < active: raise HTTPException(409, 'Limit is below current participant count')
    group.participant_limit = payload.participant_limit; group.approval_required = payload.approval_required
    db.commit()
    return snapshot(db, group, user)
