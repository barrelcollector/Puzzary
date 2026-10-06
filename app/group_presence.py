"""Live group relay; cursor traffic never writes puzzle answers."""
import asyncio
from dataclasses import dataclass, field
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal


@dataclass(eq=False)
class Peer:
    socket: WebSocket
    member: dict
    pending: dict = field(default_factory=dict)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    state_dirty: bool = False


rooms: dict[str, set[Peer]] = {}
relay_loop = None


def publish_from_http(group_id, member):
    if relay_loop and not relay_loop.is_closed():
        relay_loop.call_soon_threadsafe(broadcast, group_id, member)


def publish_state_change(group_id):
    if relay_loop and not relay_loop.is_closed():
        relay_loop.call_soon_threadsafe(mark_state_changed, group_id)


def mark_state_changed(group_id):
    for peer in rooms.get(group_id, ()):
        peer.state_dirty = True
        peer.wake.set()


def read_snapshot(socket, group_id):
    from app.groups import require_group_user, group_for, member_for, snapshot
    with SessionLocal() as db:
        group = group_for(db, group_id)
        user = require_group_user(socket, db, group)
        member_for(db, group, user)
        return snapshot(db, group, user)


def authorize(socket, group_id, value=None):
    from app.groups import require_group_user, group_for, member_for, writable, now, presence_members
    with SessionLocal() as db:
        group = group_for(db, group_id)
        user = require_group_user(socket, db, group)
        member = member_for(db, group, user)
        writable(group)
        member.seen_at = now()
        if value is not None:
            member.focus = value.get('focus', '')
            member.presence = value
        db.commit()
        members = presence_members(db, group, user)
        return next(m for m in members if m['user_id'] == user.id), members


def broadcast(group_id, member):
    # Coalesce each player's latest position instead of queuing obsolete mouse trails.
    for peer in rooms.get(group_id, ()):
        if peer.member['user_id'] != member['user_id']:
            peer.pending[member['user_id']] = member
            peer.wake.set()


async def write_updates(peer):
    while True:
        await peer.wake.wait()
        await asyncio.sleep(1 / 60)
        members = list(peer.pending.values())
        state_dirty = peer.state_dirty
        peer.state_dirty = False
        peer.pending.clear(); peer.wake.clear()
        if state_dirty:
            try:
                data = await run_in_threadpool(read_snapshot, peer.socket, peer.socket.path_params['group_id'])
            except HTTPException:
                await peer.socket.close(code=1008); return
            await asyncio.wait_for(peer.socket.send_json({'type': 'state', 'snapshot': data}), 3)
        if members:
            await asyncio.wait_for(peer.socket.send_json({'type': 'presence', 'members': members}), 3)


async def stream_presence(socket: WebSocket, group_id: str):
    global relay_loop
    relay_loop = asyncio.get_running_loop()
    from app.groups import Presence
    origin = socket.headers.get('origin')
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme not in ('http', 'https') or parsed.netloc.lower() != socket.headers.get('host', '').lower():
            await socket.close(code=1008); return
    elif not socket.headers.get('authorization', '').startswith('Bearer '):
        await socket.close(code=1008); return
    try:
        member, members = await run_in_threadpool(authorize, socket, group_id)
    except HTTPException:
        await socket.close(code=1008); return
    room = rooms.setdefault(group_id, set())
    if sum(p.member['user_id'] == member['user_id'] for p in room) >= 4:
        await socket.close(code=1008); return
    await socket.accept()
    peer = Peer(socket, member)
    known = {m['user_id']: m for m in members}
    known.update({p.member['user_id']: p.member for p in room})
    room.add(peer)
    writer = None
    try:
        data = await run_in_threadpool(read_snapshot, socket, group_id)
        await socket.send_json({'type': 'ready', 'members': list(known.values()), 'snapshot': data})
        writer = asyncio.create_task(write_updates(peer))
        checked = time.monotonic()
        window = checked; messages = 0
        while True:
            if writer.done():
                writer.result()
            try:
                raw = await asyncio.wait_for(socket.receive_text(), 2)
            except asyncio.TimeoutError:
                raw = None
            current = time.monotonic()
            if raw is not None:
                if current - window >= 1:
                    window = current; messages = 0
                messages += 1
                if messages > 90 or len(raw) > 12000:
                    await socket.close(code=1008); break
                payload = Presence.model_validate_json(raw)
                if any(len(key) > 80 for key in payload.entry):
                    await socket.close(code=1008); break
                value = {**payload.model_dump(mode='json'), 'updated_at': time.time()}
                peer.member = {**peer.member, 'online': True, 'focus': payload.focus, 'presence': value}
            if current - checked >= 2:
                member, _ = await run_in_threadpool(authorize, socket, group_id, peer.member.get('presence'))
                peer.member = {**peer.member, 'name': member['name']}
                checked = current
            if raw is not None:
                broadcast(group_id, peer.member)
    except (HTTPException, ValidationError, KeyError, ValueError):
        await socket.close(code=1008)
    except (WebSocketDisconnect, RuntimeError, asyncio.TimeoutError, OSError):
        pass
    finally:
        if writer:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        room.discard(peer)
        if not any(p.member['user_id'] == peer.member['user_id'] for p in room):
            broadcast(group_id, {**peer.member, 'presence': {}, 'focus': ''})
        if not room:
            rooms.pop(group_id, None)
