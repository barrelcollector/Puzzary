"""Persistent Group Solve sessions with opt-in, group-scoped guests."""
from datetime import datetime, timedelta, timezone
from typing import Literal
import secrets
import uuid
import hashlib
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import GroupSolve, GroupMember, GroupAction, Puzzle, User
from app.puzzles import get_puzzle_type, list_puzzle_types
from app.group_presence import publish_state_change

router = APIRouter()
SERVER_LIMIT = 50


@router.websocket('/api/groups/{group_id}/presence/ws')
async def presence_socket(socket: WebSocket, group_id: str):
    from app.group_presence import stream_presence
    await stream_presence(socket, group_id)


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
    allow_guests: bool = False


class Join(BaseModel):
    code: str = Field(min_length=1, max_length=26)


class GuestJoin(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class MoveGroup(BaseModel):
    puzzle_id: str = Field(min_length=1, max_length=26)
    revision: int = Field(ge=0)


def participant_id(member):
    return member.user_id if member.user_id is not None else -member.id


def guests_allowed(db, group):
    host = db.get(User, group.host_id)
    return bool(group.allow_guests and host and host.is_active and host.is_admin)


def require_group_user(request, db, group):
    try:
        return core().require_api_user(request, db)
    except HTTPException as error:
        if error.status_code != 401 or request.headers.get('authorization'):
            raise
    token = request.session.get('group_guests', {}).get(group.id, '')
    if not token or not guests_allowed(db, group):
        raise HTTPException(401, 'Sign in or join this group as a guest')
    member = db.scalar(select(GroupMember).where(
        GroupMember.group_id == group.id, GroupMember.user_id.is_(None),
        GroupMember.guest_token_hash == hashlib.sha256(token.encode()).hexdigest()))
    if not member:
        raise HTTPException(401, 'Guest session not found')
    return SimpleNamespace(id=-member.id, display_name=member.guest_name, is_admin=False, is_guest=True)


def member_identity(user):
    return GroupMember.id == -user.id if getattr(user, 'is_guest', False) else GroupMember.user_id == user.id


class Action(BaseModel):
    action_id: uuid.UUID
    revision: int = Field(ge=0)
    puzzle_id: str | None = Field(default=None, max_length=26)
    kind: Literal['patch', 'check', 'hint', 'reveal', 'clue', 'reveal_entry', 'check_entry'] = 'patch'
    target: str = Field(default='', max_length=80)
    changes: dict[str, str] = Field(default_factory=dict, max_length=4096)


class MemberControl(BaseModel):
    user_id: int
    action: Literal['approve', 'remove']


class Pointer(BaseModel):
    key: str = Field(default='', max_length=80)
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)


class Presence(BaseModel):
    pointer: Pointer | None = None
    focus: str = Field(default='', max_length=80)
    caret: int | None = Field(default=None, ge=0, le=16)
    selection_end: int | None = Field(default=None, ge=0, le=16)
    entry: list[str] = Field(default_factory=list, max_length=100)


def presence_members(db, group, user):
    rows = db.execute(select(GroupMember, User).outerjoin(User, GroupMember.user_id == User.id)
                      .where(GroupMember.group_id == group.id)).all()
    result = []
    for member, account in rows:
        if member.status != 'active' and user.id != group.host_id:
            continue
        online = bool(member.seen_at and (now()-utc(member.seen_at)).total_seconds() < 20)
        value = member.presence or {}
        fresh = online and now().timestamp() - value.get('updated_at', 0) < 4
        result.append({'user_id': participant_id(member), 'name': account.display_name if account else member.guest_name,
                       'guest': account is None, 'status': member.status,
                       'online': online, 'focus': member.focus if online else '',
                       'presence': value if fresh and member.status == 'active' else {}})
    return result


def group_for(db, group_id, lock=False):
    query = select(GroupSolve).where(GroupSolve.id == group_id)
    group = db.scalar(query.with_for_update() if lock else query)
    if not group: raise HTTPException(404, 'Group Solve not found')
    return group


def member_for(db, group, user, active=True):
    member = db.scalar(select(GroupMember).where(GroupMember.group_id == group.id, member_identity(user)))
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
    members = db.execute(select(GroupMember, User).outerjoin(User, GroupMember.user_id == User.id)
                         .where(GroupMember.group_id == group.id)).all()
    recent = db.scalars(select(GroupAction).where(GroupAction.group_id == group.id)
                        .where(GroupAction.revision > group.puzzle_started_revision)
                        .order_by(GroupAction.id.desc()).limit(20)).all()
    names = {participant_id(m): u.display_name if u else m.guest_name for m, u in members}
    return {'id': group.id, 'code': group.code, 'puzzle_id': group.puzzle_id,
            'host_id': group.host_id, 'user_id': user.id, 'revision': group.revision,
            'state': group.state, 'found_count': found, 'expected_count': total,
            'completed': total > 0 and found == total,
            'closed': group.closed or utc(group.expires_at) <= now(),
            'expires_at': group.expires_at.isoformat(),
            'participant_limit': group.participant_limit, 'server_limit': SERVER_LIMIT,
            'approval_required': group.approval_required,
            'allow_guests': guests_allowed(db, group),
            'can_allow_guests': user.id == group.host_id and user.is_admin,
            'is_guest': getattr(user, 'is_guest', False),
            'url': f'/puzzles/{group.puzzle_id}?group={group.id}',
            'invite_url': f'/groups/{group.id}',
            'members': presence_members(db, group, user),
            'recent': [{'revision': a.revision, 'user_id': a.user_id if a.user_id is not None else -a.guest_member_id,
                        'name': names.get(a.user_id if a.user_id is not None else -a.guest_member_id, 'Player'),
                        'kind': a.kind, 'keys': list(a.changes)} for a in recent]}


@router.post('/api/puzzles/{puzzle_id}/groups')
def create_group(puzzle_id: str, payload: GroupOptions, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    if payload.allow_guests and not user.is_admin:
        raise HTTPException(403, 'Only an admin host can allow guests')
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
    code = payload.code.strip()
    group = db.scalar(select(GroupSolve).where((GroupSolve.code == code.upper()) | (GroupSolve.id == code)).with_for_update())
    if not group: raise HTTPException(404, 'Group Solve not found')
    user = require_group_user(request, db, group)
    member = db.scalar(select(GroupMember).where(GroupMember.group_id == group.id, member_identity(user)))
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
    publish_state_change(group.id)
    return {'pending': True, 'url': f'/puzzles/{group.puzzle_id}?group={group.id}', 'id': group.id} if member.status == 'pending' else snapshot(db, group, user)


@router.post('/api/groups/{group_id}/guests')
def join_guest(group_id: str, payload: GuestJoin, request: Request, db: Session = Depends(get_db)):
    group = group_for(db, group_id, True)
    writable(group)
    if not guests_allowed(db, group):
        raise HTTPException(403, 'Guest access is disabled for this group')
    if core().current_user(request, db):
        raise HTTPException(400, 'Signed-in players should join with their account')
    try:
        user = require_group_user(request, db, group)
    except HTTPException as error:
        if error.status_code != 401:
            raise
    else:
        member_for(db, group, user, active=False)
        return join_group(Join(code=group.id), request, db)
    name = payload.name.strip()
    if not name or any(ord(c) < 32 for c in name):
        raise HTTPException(422, 'Enter a display name')
    existing = db.execute(select(GroupMember, User).outerjoin(User, GroupMember.user_id == User.id)
                          .where(GroupMember.group_id == group.id)).all()
    if any(name.casefold() == (u.display_name if u else m.guest_name or '').casefold() for m, u in existing):
        raise HTTPException(409, 'That name is already used in this group')
    if len(existing) >= 200:
        raise HTTPException(429, 'Guest join limit reached')
    active = sum(m.status == 'active' for m, _ in existing)
    if active >= (group.participant_limit or SERVER_LIMIT):
        raise HTTPException(409, 'Participant limit reached')
    token = secrets.token_urlsafe(32)
    member = GroupMember(group_id=group.id, guest_name=name,
                         guest_token_hash=hashlib.sha256(token.encode()).hexdigest(),
                         status='pending' if group.approval_required else 'active', seen_at=now())
    db.add(member)
    db.commit()
    sessions = dict(request.session.get('group_guests', {}))
    if len(sessions) >= 20:
        sessions.pop(next(iter(sessions)))
    sessions[group.id] = token
    request.session['group_guests'] = sessions
    publish_state_change(group.id)
    return {'url': f'/puzzles/{group.puzzle_id}?group={group.id}', 'pending': member.status == 'pending'}


@router.get('/api/groups/{group_id}')
def read_group(group_id: str, request: Request, focus: str = '', db: Session = Depends(get_db)):
    group = group_for(db, group_id)
    user = require_group_user(request, db, group)
    member = member_for(db, group, user)
    member.seen_at = now(); member.focus = focus[:80]
    db.commit()
    return snapshot(db, group, user)


@router.post('/api/groups/{group_id}/actions')
def group_action(group_id: str, payload: Action, request: Request, db: Session = Depends(get_db)):
    group = group_for(db, group_id, True)
    user = require_group_user(request, db, group)
    member = member_for(db, group, user); writable(group)
    if ((payload.puzzle_id is not None and payload.puzzle_id != group.puzzle_id)
            or (group.puzzle_started_revision > 0 and payload.puzzle_id is None)):
        raise HTTPException(409, 'The group moved; reload the current puzzle before editing')
    if payload.revision < group.puzzle_started_revision:
        raise HTTPException(409, 'The group moved to another puzzle; open the current puzzle')
    duplicate = db.scalar(select(GroupAction).where(GroupAction.group_id == group.id, GroupAction.action_id == str(payload.action_id)))
    if duplicate:
        owner = duplicate.user_id if duplicate.user_id is not None else -duplicate.guest_member_id
        if owner != user.id: raise HTTPException(409, 'Action ID already used')
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
    db.add(GroupAction(group_id=group.id, action_id=str(payload.action_id), user_id=member.user_id,
                       guest_member_id=member.id if member.user_id is None else None,
                       revision=group.revision, kind=payload.kind, changes=changed))
    db.commit()
    publish_state_change(group.id)
    return snapshot(db, group, user)


@router.post('/api/groups/{group_id}/presence')
def update_presence(group_id: str, payload: Presence, request: Request, db: Session = Depends(get_db)):
    group = group_for(db, group_id)
    user = require_group_user(request, db, group)
    member = member_for(db, group, user)
    writable(group)
    if any(len(key) > 80 for key in payload.entry):
        raise HTTPException(422, 'Cell key too long')
    member.focus = payload.focus
    member.seen_at = now()
    member.presence = {**payload.model_dump(mode='json'), 'updated_at': now().timestamp()}
    db.commit()
    members = presence_members(db, group, user)
    from app.group_presence import publish_from_http
    publish_from_http(group_id, next(m for m in members if m['user_id'] == user.id))
    return {'members': members}


@router.post('/api/groups/{group_id}/members')
def manage_member(group_id: str, payload: MemberControl, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True); writable(group)
    if user.id != group.host_id: raise HTTPException(403, 'Only the host can manage participants')
    if payload.user_id == group.host_id: raise HTTPException(400, 'The host cannot be removed')
    identity = ((GroupMember.id == -payload.user_id) & GroupMember.user_id.is_(None)) if payload.user_id < 0 else GroupMember.user_id == payload.user_id
    member = db.scalar(select(GroupMember).where(GroupMember.group_id == group.id, identity))
    if not member: raise HTTPException(404, 'Participant not found')
    if member.user_id == group.host_id:
        raise HTTPException(400, 'The host cannot be removed')
    if payload.action == 'approve':
        active = db.scalar(select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group.id, GroupMember.status == 'active'))
        if member.status != 'pending': raise HTTPException(400, 'No pending request')
        if active >= (group.participant_limit or SERVER_LIMIT): raise HTTPException(409, 'Participant limit reached')
        member.status = 'active'
    else: member.status = 'removed'
    db.commit()
    publish_state_change(group.id)
    return snapshot(db, group, user)


@router.post('/api/groups/{group_id}/leave')
def leave_group(group_id: str, request: Request, db: Session = Depends(get_db)):
    group = group_for(db, group_id, True)
    user = require_group_user(request, db, group)
    member = member_for(db, group, user, active=False)
    if member.status != 'removed' and user.id != group.host_id: member.status = 'left'
    member.focus = ''; member.seen_at = None; member.presence = None
    db.commit()
    publish_state_change(group.id)
    return {'left': True}


@router.post('/api/groups/{group_id}/close')
def close_group(group_id: str, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True)
    if user.id != group.host_id: raise HTTPException(403, 'Only the host can close this session')
    group.closed = True; db.commit()
    publish_state_change(group.id)
    return snapshot(db, group, user)


@router.post('/api/groups/{group_id}/settings')
def group_settings(group_id: str, payload: GroupOptions, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True); writable(group)
    if user.id != group.host_id: raise HTTPException(403, 'Only the host can change settings')
    if payload.allow_guests and not user.is_admin:
        raise HTTPException(403, 'Only an admin host can allow guests')
    active = db.scalar(select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group.id, GroupMember.status == 'active'))
    if payload.participant_limit and payload.participant_limit < active: raise HTTPException(409, 'Limit is below current participant count')
    group.participant_limit = payload.participant_limit; group.approval_required = payload.approval_required
    group.allow_guests = payload.allow_guests
    if not group.allow_guests:
        for member in db.scalars(select(GroupMember).where(GroupMember.group_id == group.id, GroupMember.user_id.is_(None))):
            member.presence = None
            member.seen_at = None
            if member.status != 'removed':
                member.status = 'left'
    db.commit()
    publish_state_change(group.id)
    return snapshot(db, group, user)


@router.get('/api/groups/{group_id}/puzzles')
def group_puzzles(group_id: str, request: Request, q: str = '', offset: int = 0, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id)
    if user.id != group.host_id:
        raise HTTPException(403, 'Only the host can choose puzzles')
    supported = [p.slug for p in list_puzzle_types() if p.collaboration]
    query = select(Puzzle).where(Puzzle.puzzle_type.in_(supported), Puzzle.archived_at.is_(None),
                                (Puzzle.auto_archive_at.is_(None)) | (Puzzle.auto_archive_at > now()))
    if q.strip():
        query = query.where(Puzzle.title.icontains(q.strip()[:160], autoescape=True))
    puzzles = db.scalars(query.order_by(Puzzle.created_at.desc(), Puzzle.id).offset(max(0, offset)).limit(51)).all()
    return {'puzzles': [{'id': p.id, 'title': p.title, 'puzzle_type': p.puzzle_type,
                         'difficulty': p.difficulty, 'visited': p.id in (group.puzzle_states or {}) or p.id == group.puzzle_id}
                        for p in puzzles[:50]], 'has_more': len(puzzles) > 50}


@router.post('/api/groups/{group_id}/puzzle')
def move_group(group_id: str, payload: MoveGroup, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True)
    if user.id != group.host_id:
        raise HTTPException(403, 'Only the host can move the group')
    writable(group)
    if payload.revision != group.revision:
        raise HTTPException(409, 'Group changed; synchronize before moving')
    puzzle = db.get(Puzzle, payload.puzzle_id)
    if not puzzle:
        raise HTTPException(404, 'Puzzle not found')
    if puzzle.archived_at or (puzzle.auto_archive_at and utc(puzzle.auto_archive_at) <= now()):
        raise HTTPException(409, 'Choose an active puzzle')
    plugin = get_puzzle_type(puzzle.puzzle_type)
    if not plugin or not plugin.collaboration:
        raise HTTPException(400, 'This puzzle type does not support Group Solve')
    if puzzle.id == group.puzzle_id:
        return snapshot(db, group, user)
    history = {**(group.puzzle_states or {}), group.puzzle_id: group.state}
    if puzzle.id not in history and len(history) >= 100:
        raise HTTPException(409, 'This group has reached its 100-puzzle limit')
    group.puzzle_states = history
    group.puzzle_id = puzzle.id
    group.state = history.get(puzzle.id, {'answers': {}})
    group.revision += 1
    group.puzzle_started_revision = group.revision
    group.cell_versions = {key: group.revision for key in group.state.get('answers', {})}
    for member in db.scalars(select(GroupMember).where(GroupMember.group_id == group.id)):
        member.focus = ''
        member.presence = None
    db.commit()
    publish_state_change(group.id)
    return snapshot(db, group, user)
