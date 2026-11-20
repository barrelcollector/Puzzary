"""Isolated API and browser QA for the opt-in public Group Solve lobby."""
import os
from qa_runtime import qa_directory

qa_root = qa_directory('group-lobby')
os.environ['DATABASE_URL'] = f"sqlite:///{(qa_root / 'group-lobby.sqlite').as_posix()}"
os.environ['SESSION_SECRET'] = 'isolated-group-lobby-secret'
os.environ['UPLOAD_DIR'] = str(qa_root / 'uploads')

import base64
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
import time
import uuid

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright

from app.database import Base, SessionLocal, engine
from app.main import SESSION_SECRET, hash_password
from app.models import GroupSolve, Puzzle, User
from app.puzzles import get_puzzle_type


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
with SessionLocal() as db:
    host = User(display_name='Lobby QA Host', is_admin=True, is_active=True, session_version=1,
                password_hash=hash_password('Account-test-123'))
    second_host = User(display_name='Lobby QA Publisher', is_admin=False, is_active=True, session_version=1,
                       password_hash=hash_password('Account-test-123'))
    db.add_all([host, second_host]); db.flush(); host_id, second_host_id = host.id, second_host.id
    generated = get_puzzle_type('takuzu').manual_builder({
        'solution_grid': [[0, 0, 1, 1], [0, 1, 0, 1], [1, 0, 1, 0], [1, 1, 0, 0]],
        'given_count': 0, 'seed': 'group-lobby-qa',
    })
    for puzzle_id, title, metadata, archived in [
        ('lobby-public', 'Lobby Public Puzzle', {}, None),
        ('lobby-draft', 'Secret Draft Puzzle', {'draft': True}, None),
        ('lobby-hidden', 'Secret Hidden Puzzle', {'visibility': 'hidden'}, None),
        ('lobby-archived', 'Secret Archived Puzzle', {}, datetime.now(timezone.utc)),
    ]:
        db.add(Puzzle(id=puzzle_id, puzzle_type='takuzu', title=title, theme='', difficulty='easy',
                      prompt=generated.prompt, play_payload=generated.play_payload,
                      print_payload=generated.print_payload, solution_payload=generated.solution_payload,
                      puzzle_metadata=metadata, source='test', archived_at=archived))
    db.commit()


def cookie(user_id):
    payload = base64.b64encode(json.dumps({'user_id': user_id, 'session_version': 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


base = 'http://127.0.0.1:8795'
server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8795'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        host = browser.new_context(viewport={'width': 1280, 'height': 900})
        publisher = browser.new_context()
        anonymous = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        host.add_cookies([{'name': 'session', 'value': cookie(host_id), 'url': base}])
        publisher.add_cookies([{'name': 'session', 'value': cookie(second_host_id), 'url': base}])
        for _ in range(100):
            try:
                if anonymous.request.get(base + '/health').ok:
                    break
            except Exception:
                time.sleep(.1)
        else:
            raise RuntimeError('QA server did not start')

        lobby_api = base + '/api/groups/lobby'
        assert anonymous.request.get(lobby_api).json()['groups'] == []
        create = base + '/api/puzzles/lobby-public/groups'
        private = host.request.post(create, data={'allow_guests': True, 'password': 'Lobby-Guest-123'}).json()
        assert private['public_lobby'] is False
        assert anonymous.request.get(lobby_api).json()['groups'] == []

        settings = base + '/api/groups/' + private['id'] + '/settings'
        listed = host.request.post(settings, data={
            'allow_guests': True, 'password': '', 'public_lobby': True,
            'participant_limit': 2, 'approval_required': False,
        })
        assert listed.ok and listed.json()['public_lobby'], listed.text()
        lobby_response = anonymous.request.get(lobby_api)
        assert lobby_response.headers['cache-control'] == 'no-store'
        public = lobby_response.json()['groups']
        assert len(public) == 1
        item = public[0]
        assert item['puzzle_title'] == 'Lobby Public Puzzle' and item['host_name'] == 'Lobby QA Host'
        assert item['players'] == 1 and item['capacity'] == 2 and item['status'] == 'open' and item['joinable']
        assert item['password_protected'] and item['guest_allowed']
        assert set(item) == {'id', 'url', 'puzzle_title', 'puzzle_type', 'puzzle_type_label', 'host_name',
                             'players', 'capacity', 'password_protected', 'guest_allowed', 'joinable',
                             'puzzle_password_protected', 'status', 'status_label', 'created_at', 'activity_at'}
        assert item['puzzle_password_protected'] is False
        serialized = json.dumps(item)
        assert private['code'] not in serialized and 'Lobby-Guest-123' not in serialized
        assert 'state' not in item and 'progress' not in item and 'solution' not in item

        assert len(anonymous.request.get(lobby_api + '?q=public&puzzle_type=takuzu&available=open&password=required').json()['groups']) == 1
        assert anonymous.request.get(lobby_api + '?q=missing').json()['groups'] == []
        assert anonymous.request.get(lobby_api + '?password=not_required').json()['groups'] == []
        lobby = anonymous.new_page(); page_response = lobby.goto(base + '/groups')
        assert page_response.headers['cache-control'] == 'no-store'
        assert lobby.locator('.group-lobby-card').count() == 1
        assert lobby.locator('.group-lobby-card').inner_text().find(private['code']) == -1
        assert 'Guest password required' in lobby.locator('.group-lobby-card').inner_text()
        assert lobby.evaluate('document.documentElement.scrollWidth <= innerWidth')
        lobby.locator('.group-lobby-card .button-link').click()
        assert lobby.locator('#guest-join [name=password]').count() == 1
        lobby.locator('[name=name]').fill('Lobby Guest')
        lobby.locator('[name=password]').fill('Lobby-Guest-123')
        lobby.locator('#guest-join button').click()
        lobby.wait_for_function("document.querySelector('#group-solve')?.dataset.revision !== undefined")
        full = anonymous.request.get(lobby_api).json()['groups'][0]
        assert full['status'] == 'full' and not full['joinable'] and full['players'] == 2
        assert len(anonymous.request.get(lobby_api + '?available=full').json()['groups']) == 1

        reopened = host.request.post(settings, data={
            'allow_guests': True, 'public_lobby': True, 'participant_limit': 3,
        })
        assert reopened.ok
        action = host.request.post(base + '/api/groups/' + private['id'] + '/actions', data={
            'action_id': str(uuid.uuid4()), 'revision': reopened.json()['revision'],
            'puzzle_id': 'lobby-public', 'kind': 'patch', 'target': '', 'changes': {'0,0': '0'},
        })
        assert action.ok
        progress = anonymous.request.get(lobby_api).json()['groups'][0]
        assert progress['status'] == 'in_progress' and progress['joinable']

        hidden_groups = []
        for puzzle_id in ['lobby-draft', 'lobby-hidden', 'lobby-archived']:
            with SessionLocal() as db:
                group = GroupSolve(id=uuid.uuid4().hex[:24], code=uuid.uuid4().hex[:10].upper(),
                                   puzzle_id=puzzle_id, host_id=host_id, state={'answers': {}}, cell_versions={},
                                   public_lobby=True, expires_at=datetime.now(timezone.utc) + timedelta(days=1))
                db.add(group); db.commit(); hidden_groups.append(group.id)
        hidden_response = json.dumps(anonymous.request.get(lobby_api).json())
        assert all(group_id not in hidden_response for group_id in hidden_groups)
        assert 'Secret' not in hidden_response

        other = publisher.request.post(create, data={}).json()
        other_settings = base + '/api/groups/' + other['id'] + '/settings'
        assert host.request.post(other_settings, data={'public_lobby': True}).ok
        assert other['id'] in {group['id'] for group in anonymous.request.get(lobby_api).json()['groups']}
        assert host.request.post(base + '/api/groups/join', data={'code': other['code']}).ok
        transferred = publisher.request.post(base + '/api/groups/' + other['id'] + '/host', data={'user_id': host_id})
        assert transferred.ok and not transferred.json()['public_lobby']
        assert other['id'] not in {group['id'] for group in anonymous.request.get(lobby_api).json()['groups']}

        live_lobby = anonymous.new_page(); live_lobby.goto(base + '/groups')
        assert live_lobby.locator('.group-lobby-card').count() == 1
        delisted = host.request.post(settings, data={
            'allow_guests': True, 'public_lobby': False, 'participant_limit': 3,
        })
        assert delisted.ok and not delisted.json()['public_lobby']
        assert private['id'] not in {group['id'] for group in anonymous.request.get(lobby_api).json()['groups']}
        live_lobby.locator('#group-lobby-filters button[type=submit]').click()
        live_lobby.wait_for_function("document.querySelector('#group-lobby-status').textContent === '0 public sessions'")
        assert live_lobby.locator('.group-lobby-card').count() == 0
        assert anonymous.request.get(base + '/api/groups/' + private['id']).ok
        assert host.request.post(settings, data={
            'allow_guests': True, 'public_lobby': True, 'participant_limit': 3,
        }).ok
        assert host.request.post(base + '/api/groups/' + private['id'] + '/close', data={}).ok
        assert private['id'] not in {group['id'] for group in anonymous.request.get(lobby_api).json()['groups']}

        limited = None
        for _ in range(65):
            response = anonymous.request.get(lobby_api)
            if response.status == 429:
                limited = response
                break
        assert limited is not None and int(limited.headers['retry-after']) > 0
        browser.close()
        print('Public lobby opt-in, privacy, filters, statuses, normal guest flow, RBAC, delisting, hidden puzzles, rate limiting and mobile UI passed.')
finally:
    server.terminate()
    server.wait(timeout=15)
