"""Persistent Group Solve sessions with opt-in, group-scoped guests."""
from datetime import datetime, timedelta, timezone
from typing import Literal
from copy import deepcopy
import os
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
HOST_RECONNECT_GRACE_SECONDS = max(1, min(300, int(os.getenv('GROUP_HOST_RECONNECT_GRACE_SECONDS', '30'))))
MEMBER_ONLINE_SECONDS = 20


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
    puzzle_options: dict[str, str] = Field(default_factory=dict, max_length=20)


class Join(BaseModel):
    code: str = Field(min_length=1, max_length=26)


class GuestJoin(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class MoveGroup(BaseModel):
    puzzle_id: str = Field(min_length=1, max_length=26)
    revision: int = Field(ge=0)


def participant_id(member):
    return member.user_id if member.user_id is not None else -member.id


def participant_color(index):
    """Stable, session-unique colors; text labels keep color from being the only cue."""
    hue = (index * 137.508 + 188) % 360
    return f'hsl({hue:.1f} 68% 34%)'


def guests_allowed(db, group):
    host = db.get(User, group.host_id)
    return bool(group.allow_guests and host and host.can('groups.guests'))


def can_manage_group(user, group):
    return not getattr(user, 'is_guest', False) and (user.can('groups.manage_any') or (user.id == group.host_id and user.can('groups.moderate')))


def require_group_user(request, db, group):
    try:
        user = core().require_api_user(request, db)
        from app.permissions import can_view, require
        require(user, 'groups.join')
        if not can_view(user, db.get(Puzzle, group.puzzle_id), play=True):
            raise HTTPException(403, 'Puzzle access denied')
        return user
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


class HostTransfer(BaseModel):
    user_id: int = Field(gt=0)


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
                      .where(GroupMember.group_id == group.id).order_by(GroupMember.id)).all()
    result = []
    for color_index, (member, account) in enumerate(rows):
        if member.status != 'active' and not can_manage_group(user,group):
            continue
        online = bool(member.seen_at and (now()-utc(member.seen_at)).total_seconds() < MEMBER_ONLINE_SECONDS)
        value = member.presence or {}
        fresh = online and now().timestamp() - value.get('updated_at', 0) < 4
        result.append({'user_id': participant_id(member), 'name': account.display_name if account else member.guest_name,
                       'guest': account is None, 'status': member.status,
                       'color': participant_color(color_index), 'marker': color_index + 1,
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


def host_capable(user):
    return bool(user and user.is_active and user.can('groups.host') and user.can('groups.moderate'))


def host_candidates(db, group, *, connected_only=True, exclude_user_ids=()):
    cutoff = now() - timedelta(seconds=MEMBER_ONLINE_SECONDS)
    rows = db.execute(
        select(GroupMember, User).join(User, GroupMember.user_id == User.id)
        .where(GroupMember.group_id == group.id, GroupMember.status == 'active')
        .order_by(GroupMember.id)
    ).all()
    excluded = set(exclude_user_ids)
    return [
        (member, account) for member, account in rows
        if account.id not in excluded and host_capable(account)
        and (not connected_only or (member.seen_at is not None and utc(member.seen_at) >= cutoff))
    ]


def change_host(group, target, *, reason):
    previous = group.host_id
    if target is None:
        group.closed = True
        group.revision += 1
        return {'previous_host_id': previous, 'host_id': None, 'reason': reason, 'closed': True}
    group.host_id = target.user_id
    group.revision += 1
    return {'previous_host_id': previous, 'host_id': target.user_id, 'reason': reason, 'closed': False}


def transfer_after_departure(db, group, departing_user_id, *, reason, connected_only=True):
    candidates = host_candidates(db, group, connected_only=connected_only, exclude_user_ids={departing_user_id})
    return change_host(group, candidates[0][0] if candidates else None, reason=reason)


def maybe_transfer_disconnected_host(db, group):
    if group.closed:
        return None
    host_member = db.scalar(select(GroupMember).where(
        GroupMember.group_id == group.id, GroupMember.user_id == group.host_id
    ))
    cutoff = now() - timedelta(seconds=HOST_RECONNECT_GRACE_SECONDS)
    if host_member and host_member.status == 'active' and host_member.seen_at is not None and utc(host_member.seen_at) >= cutoff:
        return None
    departing_user_id = group.host_id
    if host_member and host_member.status == 'active':
        host_member.status = 'left'
        host_member.focus = ''
        host_member.presence = None
    return transfer_after_departure(db, group, departing_user_id, reason='disconnected')


def writable(group):
    if group.closed or utc(group.expires_at) <= now():
        raise HTTPException(409, 'This Group Solve is closed or expired')


def collaboration_context(db, group):
    return {'participants': [participant_id(m) for m in db.scalars(
        select(GroupMember).where(GroupMember.group_id == group.id, GroupMember.status == 'active')
        .order_by(GroupMember.id))]}


def configure_collaboration(plugin, state, options, solution):
    if any(len(k) > 80 or len(v) > 80 for k, v in options.items()):
        raise HTTPException(422, 'Group option too long')
    if options and not plugin.collaboration_setup:
        raise HTTPException(422, 'This puzzle has no group options')
    try:
        return plugin.collaboration_setup(state, options, solution) if plugin.collaboration_setup else state
    except ValueError as error:
        raise HTTPException(422, str(error))


def attribution_events(plugin, before, after, action, changed):
    """Return public contribution facts without copying answer values or solution data."""
    events = [{'key': key, 'type': 'answer', 'label': '', 'current': bool(value)}
              for key, value in changed.items()]
    before_words = {str(value) for value in before.get('found_words', [])}
    for word in sorted({str(value) for value in after.get('found_words', [])} - before_words):
        events.append({'key': f'word:{word}', 'type': 'found_word', 'label': word, 'current': True})
    before_units = {str(value) for value in before.get('progressive_completed', [])}
    for unit in sorted({str(value) for value in after.get('progressive_completed', [])} - before_units):
        events.append({'key': f'unit:{unit}', 'type': 'completed_unit', 'label': unit.replace(':', ' '), 'current': True})
    before_states = before.get('states', [])
    for index, value in enumerate(after.get('states', [])):
        if value == 'correct' and (index >= len(before_states) or before_states[index] != 'correct'):
            events.append({'key': f'entry:{index}', 'type': 'completed_entry', 'label': f'Entry {index + 1}', 'current': True})
    if plugin.collaboration_attribution:
        events.extend(plugin.collaboration_attribution(before, after, action))
    clean = []
    seen = set()
    for event in events:
        key = str(event.get('key', ''))[:80]
        event_type = str(event.get('type', 'action'))[:32]
        if not key or (key, event_type) in seen:
            continue
        seen.add((key, event_type))
        clean.append({'key': key, 'type': event_type, 'label': str(event.get('label', ''))[:80],
                      'current': bool(event.get('current', True))})
    return clean


def apply_attribution(state, events, actor, kind, revision, created_at):
    attribution = deepcopy(state.get('_attribution', {}))
    owners = dict(attribution.get('owners', {}))
    for event in events:
        if event['current']:
            owners[event['key']] = {'user_id': actor, 'kind': kind, 'revision': revision,
                                    'at': created_at.isoformat(), 'type': event['type'], 'label': event['label']}
        else:
            owners.pop(event['key'], None)
    attribution['owners'] = owners
    contributors = dict(attribution.get('contributors', {}))
    item = dict(contributors.get(str(actor), {'user_id': actor, 'actions': 0, 'edits': 0,
        'found_words': 0, 'completed_entries': 0, 'hints': 0, 'reveals': 0, 'checks': 0, 'clues': 0}))
    item['actions'] += 1
    item['edits'] += len(events)
    item['found_words'] += sum(event['type'] == 'found_word' for event in events)
    item['completed_entries'] += sum(event['type'] in ('completed_entry', 'completed_unit') for event in events)
    category = ('reveals' if kind in ('reveal', 'reveal_entry') else
                'checks' if kind in ('check', 'check_entry') else f'{kind}s')
    if category in item:
        item[category] += 1
    item['last_at'] = created_at.isoformat()
    contributors[str(actor)] = item
    attribution['contributors'] = contributors
    state['_attribution'] = attribution
    return state


def snapshot(db, group, user):
    puzzle = db.get(Puzzle, group.puzzle_id)
    plugin = get_puzzle_type(puzzle.puzzle_type)
    if not plugin or not plugin.collaboration: raise HTTPException(410, 'Collaboration plugin is unavailable')
    context = collaboration_context(db, group)
    found, total = plugin.progress_counter({**group.state, '_participants': context['participants']}, puzzle.solution_payload)
    members = db.execute(select(GroupMember, User).outerjoin(User, GroupMember.user_id == User.id)
                         .where(GroupMember.group_id == group.id).order_by(GroupMember.id)).all()
    recent = db.scalars(select(GroupAction).where(GroupAction.group_id == group.id)
                        .where(GroupAction.revision > group.puzzle_started_revision)
                        .order_by(GroupAction.id.desc()).limit(20)).all()
    names = {participant_id(m): u.display_name if u else m.guest_name for m, u in members}
    styles = {participant_id(m): {'color': participant_color(index), 'marker': index + 1}
              for index, (m, _u) in enumerate(members)}
    contributor_data = {}
    for raw in group.state.get('_attribution', {}).get('contributors', {}).values():
        actor = raw.get('user_id')
        contributor_data[actor] = {**raw, 'name': names.get(actor, 'Player'),
            **styles.get(actor, {'color': participant_color(len(styles)), 'marker': len(styles) + 1}), 'current': 0}
    for owner in group.state.get('_attribution', {}).get('owners', {}).values():
        actor = owner.get('user_id')
        if actor not in contributor_data:
            contributor_data[actor] = {'user_id': actor, 'name': names.get(actor, 'Player'),
                **styles.get(actor, {'color': participant_color(len(styles)), 'marker': len(styles) + 1}),
                'actions': 0, 'edits': 0, 'found_words': 0, 'completed_entries': 0,
                'hints': 0, 'reveals': 0, 'checks': 0, 'clues': 0, 'current': 0, 'last_at': owner.get('at')}
        contributor_data[actor]['current'] += 1
    transfer_candidates = [member.user_id for member, _account in host_candidates(db, group)] if can_manage_group(user, group) else []
    return {'id': group.id, 'code': group.code, 'puzzle_id': group.puzzle_id,
            'host_id': group.host_id, 'user_id': user.id, 'revision': group.revision,
            'puzzle_epoch': group.puzzle_started_revision,
            'state': group.state, 'found_count': found, 'expected_count': total,
            'context': context,
            'completion_message': puzzle.completion_message or '',
            'completed': total > 0 and found == total,
            'closed': group.closed or utc(group.expires_at) <= now(),
            'expires_at': group.expires_at.isoformat(),
            'participant_limit': group.participant_limit, 'server_limit': SERVER_LIMIT,
            'approval_required': group.approval_required,
            'allow_guests': guests_allowed(db, group),
            'can_allow_guests': can_manage_group(user, group) and user.can('groups.guests'),
            'can_moderate': can_manage_group(user,group),
            'can_transfer_host': can_manage_group(user, group),
            'host_transfer_candidates': transfer_candidates,
            'host_reconnect_grace_seconds': HOST_RECONNECT_GRACE_SECONDS,
            'is_guest': getattr(user, 'is_guest', False),
            'url': f'/puzzles/{group.puzzle_id}?group={group.id}',
            'invite_url': f'/groups/{group.id}',
            'members': presence_members(db, group, user),
            'contributors': sorted(contributor_data.values(), key=lambda item: (-item['current'], -item['actions'], item['name'].casefold())),
            'recent': [{'revision': a.revision, 'user_id': a.user_id if a.user_id is not None else -a.guest_member_id,
                        'name': names.get(a.user_id if a.user_id is not None else -a.guest_member_id, 'Player'),
                        'kind': a.kind, 'target': a.target, 'at': utc(a.created_at).isoformat(),
                        'keys': ([event.get('key') for event in (a.contributions or []) if event.get('key')]
                                 or list(a.changes or {}))}
                       for a in recent]}


@router.post('/api/puzzles/{puzzle_id}/groups')
def create_group(puzzle_id: str, payload: GroupOptions, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    if payload.allow_guests and not user.can('groups.guests'):
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
                       expires_at=now()+timedelta(days=7), **payload.model_dump(exclude={'puzzle_options'}))
    group.state = configure_collaboration(plugin, group.state, payload.puzzle_options, puzzle.solution_payload)
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
    host_change = maybe_transfer_disconnected_host(db, group)
    db.commit()
    if host_change or member.status == 'active': publish_state_change(group.id)
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
    group = group_for(db, group_id, True)
    user = require_group_user(request, db, group)
    member = member_for(db, group, user)
    member.seen_at = now(); member.focus = focus[:80]
    host_change = maybe_transfer_disconnected_host(db, group)
    db.commit()
    if host_change: publish_state_change(group.id)
    return snapshot(db, group, user)


@router.get('/api/groups/{group_id}/invite')
def group_invitation(group_id: str, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db,group_id)
    member_for(db,group,user)
    return {'url':f'/groups/{group.id}'}


@router.post('/api/groups/{group_id}/actions')
def group_action(group_id: str, payload: Action, request: Request, db: Session = Depends(get_db)):
    group = group_for(db, group_id, True)
    user = require_group_user(request, db, group)
    member = member_for(db, group, user); writable(group)
    member.seen_at = now()
    host_change = maybe_transfer_disconnected_host(db, group)
    if host_change and group.closed:
        db.commit()
        publish_state_change(group.id)
        raise HTTPException(409, 'This Group Solve closed because no eligible host remained')
    writable(group)
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
    puzzle = db.get(Puzzle, group.puzzle_id)
    plugin = get_puzzle_type(puzzle.puzzle_type)
    if not plugin or not plugin.collaboration: raise HTTPException(400, 'Plugin no longer supports Group Solve')
    if sum(len(k) + len(v) for k, v in payload.changes.items()) > 65536:
        raise HTTPException(413, 'Group patch is too large')
    if any(len(k) > 80 or len(v) > plugin.collaboration_value_limit for k, v in payload.changes.items()):
        raise HTTPException(422, 'Cell value too long')
    action = {**payload.model_dump(mode='json'), '_actor': user.id, **collaboration_context(db, group)}
    before_state = deepcopy(group.state)
    try: state = plugin.collaboration(group.state, action, puzzle.solution_payload)
    except ValueError as error: raise HTTPException(422, str(error))
    before = before_state.get('answers', {})
    after = state.get('answers', {})
    changed = {k: after.get(k, '') for k in set(before) | set(after) if before.get(k, '') != after.get(k, '')}
    group.revision += 1
    actor = participant_id(member)
    created_at = now()
    contributions = attribution_events(plugin, before_state, state, action, changed)
    state = apply_attribution(state, contributions, actor, payload.kind, group.revision, created_at)
    group.state = state
    group.cell_versions = {**group.cell_versions, **{k: group.revision for k in changed}}
    db.add(GroupAction(group_id=group.id, action_id=str(payload.action_id), user_id=member.user_id,
                       guest_member_id=member.id if member.user_id is None else None,
                       revision=group.revision, kind=payload.kind, target=payload.target,
                       changes=changed, contributions=contributions, created_at=created_at))
    db.commit()
    publish_state_change(group.id)
    return snapshot(db, group, user)


@router.post('/api/groups/{group_id}/presence')
def update_presence(group_id: str, payload: Presence, request: Request, db: Session = Depends(get_db)):
    group = group_for(db, group_id, True)
    user = require_group_user(request, db, group)
    member = member_for(db, group, user)
    writable(group)
    if any(len(key) > 80 for key in payload.entry):
        raise HTTPException(422, 'Cell key too long')
    member.focus = payload.focus
    member.seen_at = now()
    member.presence = {**payload.model_dump(mode='json'), 'updated_at': now().timestamp()}
    host_change = maybe_transfer_disconnected_host(db, group)
    db.commit()
    if host_change: publish_state_change(group.id)
    members = presence_members(db, group, user)
    from app.group_presence import publish_from_http
    publish_from_http(group_id, next(m for m in members if m['user_id'] == user.id))
    return {'members': members}


@router.post('/api/groups/{group_id}/members')
def manage_member(group_id: str, payload: MemberControl, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True); writable(group)
    if not can_manage_group(user, group): raise HTTPException(403, 'Group moderation permission required')
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


@router.post('/api/groups/{group_id}/host')
def transfer_host(group_id: str, payload: HostTransfer, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True); writable(group)
    if not can_manage_group(user, group):
        raise HTTPException(403, 'Group moderation permission required')
    if payload.user_id == group.host_id:
        raise HTTPException(422, 'That player is already the host')
    member = db.scalar(select(GroupMember).where(
        GroupMember.group_id == group.id, GroupMember.user_id == payload.user_id
    ))
    target = db.get(User, payload.user_id)
    if not member or member.status != 'active' or not host_capable(target):
        raise HTTPException(422, 'Choose an eligible signed-in participant')
    if member.seen_at is None or (now() - utc(member.seen_at)).total_seconds() >= MEMBER_ONLINE_SECONDS:
        raise HTTPException(409, 'That participant is no longer connected')
    change_host(group, member, reason='voluntary')
    db.commit()
    publish_state_change(group.id)
    return snapshot(db, group, user)


@router.post('/api/groups/{group_id}/leave')
def leave_group(group_id: str, request: Request, db: Session = Depends(get_db)):
    group = group_for(db, group_id, True)
    user = require_group_user(request, db, group)
    member = member_for(db, group, user, active=False)
    was_host = not getattr(user, 'is_guest', False) and user.id == group.host_id
    if member.status != 'removed': member.status = 'left'
    member.focus = ''; member.seen_at = None; member.presence = None
    host_change = transfer_after_departure(db, group, user.id, reason='left', connected_only=False) if was_host else None
    db.commit()
    publish_state_change(group.id)
    return {'left': True, 'host_change': host_change}


@router.post('/api/groups/{group_id}/close')
def close_group(group_id: str, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True)
    if not can_manage_group(user, group): raise HTTPException(403, 'Group moderation permission required')
    group.closed = True; db.commit()
    publish_state_change(group.id)
    return snapshot(db, group, user)


@router.post('/api/groups/{group_id}/settings')
def group_settings(group_id: str, payload: GroupOptions, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True); writable(group)
    if not can_manage_group(user, group): raise HTTPException(403, 'Group moderation permission required')
    if payload.allow_guests and (not user.can('groups.guests') or not db.get(User, group.host_id).can('groups.guests')):
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
    puzzle = db.get(Puzzle, group.puzzle_id)
    plugin = get_puzzle_type(puzzle.puzzle_type)
    previous = group.state
    group.state = configure_collaboration(plugin, previous, payload.puzzle_options, puzzle.solution_payload)
    if group.state != previous:
        group.state.pop('_attribution', None)
        group.revision += 1
        group.puzzle_started_revision = group.revision
        group.cell_versions = {k: group.revision for k in set(previous.get('answers', {})) | set(group.state.get('answers', {}))}
    db.commit()
    publish_state_change(group.id)
    return snapshot(db, group, user)


@router.get('/api/groups/{group_id}/puzzles')
def group_puzzles(group_id: str, request: Request, q: str = '', offset: int = 0, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id)
    if not can_manage_group(user, group):
        raise HTTPException(403, 'Only the host can choose puzzles')
    supported = [p.slug for p in list_puzzle_types() if p.collaboration]
    query = select(Puzzle).where(Puzzle.puzzle_type.in_(supported), Puzzle.archived_at.is_(None),
                                (Puzzle.auto_archive_at.is_(None)) | (Puzzle.auto_archive_at > now()))
    if q.strip():
        query = query.where(Puzzle.title.icontains(q.strip()[:160], autoescape=True))
    from app.permissions import can_view
    puzzles = [p for p in db.scalars(query.order_by(Puzzle.created_at.desc(), Puzzle.id)) if can_view(user,p,play=True)]
    puzzles = puzzles[max(0,offset):max(0,offset)+51]
    return {'puzzles': [{'id': p.id, 'title': p.title, 'puzzle_type': p.puzzle_type,
                         'difficulty': p.difficulty, 'visited': p.id in (group.puzzle_states or {}) or p.id == group.puzzle_id}
                        for p in puzzles[:50]], 'has_more': len(puzzles) > 50}


@router.post('/api/groups/{group_id}/puzzle')
def move_group(group_id: str, payload: MoveGroup, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    group = group_for(db, group_id, True)
    if not can_manage_group(user, group):
        raise HTTPException(403, 'Only the host can move the group')
    writable(group)
    if payload.revision != group.revision:
        raise HTTPException(409, 'Group changed; synchronize before moving')
    puzzle = db.get(Puzzle, payload.puzzle_id)
    if not puzzle:
        raise HTTPException(404, 'Puzzle not found')
    from app.permissions import can_view
    if not can_view(user, puzzle, play=True): raise HTTPException(403, 'Puzzle access denied')
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
    group.state = history.get(puzzle.id) or configure_collaboration(plugin, {'answers': {}}, {}, puzzle.solution_payload)
    group.revision += 1
    group.puzzle_started_revision = group.revision
    group.cell_versions = {key: group.revision for key in group.state.get('answers', {})}
    for member in db.scalars(select(GroupMember).where(GroupMember.group_id == group.id)):
        member.focus = ''
        member.presence = None
    db.commit()
    publish_state_change(group.id)
    return snapshot(db, group, user)
