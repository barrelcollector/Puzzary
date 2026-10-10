"""Isolated API, WebSocket and browser QA for Group Solve guest passwords."""
import os
os.environ['DATABASE_URL'] = 'sqlite:////tmp/puzzary-group-password-qa.sqlite'
os.environ['SESSION_SECRET'] = 'isolated-group-password-secret'
os.environ['UPLOAD_DIR'] = '/tmp/group-password-qa-uploads'

import base64
import json
import subprocess
import time

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from app.database import Base, SessionLocal, engine
from app.main import SESSION_SECRET, hash_password, verify_password
from app.models import GroupSolve, Puzzle, User
from app.puzzles import get_puzzle_type


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
with SessionLocal() as db:
    host = User(display_name='Password QA Host', is_admin=True, is_active=True, session_version=1,
                password_hash=hash_password('Account-test-123'))
    player = User(display_name='Password QA Player', is_admin=False, is_active=True, session_version=1,
                  password_hash=hash_password('Account-test-123'))
    db.add_all([host, player]); db.flush(); host_id, player_id = host.id, player.id
    generated = get_puzzle_type('takuzu').manual_builder({
        'solution_grid': [[0, 0, 1, 1], [0, 1, 0, 1], [1, 0, 1, 0], [1, 1, 0, 0]],
        'given_count': 0, 'seed': 'group-password-qa',
    })
    db.add(Puzzle(id='password-qa', puzzle_type='takuzu', title='Password QA', theme='', difficulty='easy',
                  prompt=generated.prompt, play_payload=generated.play_payload,
                  print_payload=generated.print_payload, solution_payload=generated.solution_payload,
                  puzzle_metadata={}, source='test'))
    db.commit()


def cookie(user_id):
    payload = base64.b64encode(json.dumps({'user_id': user_id, 'session_version': 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


base = 'http://127.0.0.1:8794'
server = subprocess.Popen(['uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8794'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        host = browser.new_context(viewport={'width': 1280, 'height': 900})
        player = browser.new_context()
        host.add_cookies([{'name': 'session', 'value': cookie(host_id), 'url': base}])
        player.add_cookies([{'name': 'session', 'value': cookie(player_id), 'url': base}])
        for _ in range(100):
            try:
                if host.request.get(base + '/health').ok:
                    break
            except Exception:
                time.sleep(.1)
        else:
            raise RuntimeError('QA server did not start')

        create = base + '/api/puzzles/password-qa/groups'
        assert host.request.post(create, data={'allow_guests': True, 'password': 'short'}).status == 422
        assert host.request.post(create, data={'allow_guests': True, 'remove_password': True}).status == 422
        created = host.request.post(create, data={'allow_guests': True, 'password': 'Guest-Access-123'}).json()
        endpoint = base + '/api/groups/' + created['id']
        assert created['password_protected'] and 'password' not in created and 'password_hash' not in created
        assert player.request.post(base + '/api/groups/join', data={'code': created['code']}).ok
        with SessionLocal() as db:
            stored = db.get(GroupSolve, created['id']).password_hash
            assert stored and stored != 'Guest-Access-123' and verify_password('Guest-Access-123', stored)

        stranger = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        assert stranger.request.get(endpoint).status == 401
        ws_url = endpoint.replace('http:', 'ws:') + '/presence/ws'
        try:
            with connect(ws_url, origin=base, proxy=None):
                raise AssertionError('Unjoined guest WebSocket accepted')
        except InvalidStatus as error:
            assert error.response.status_code == 403
        invite = stranger.new_page(); invite.goto(base + created['url'])
        assert invite.locator('#guest-join [name=password]').get_attribute('type') == 'password'
        assert invite.locator('#guest-join [name=password]').get_attribute('required') is not None
        assert 'Guest-Access-123' not in invite.content() and stored not in invite.content()
        assert stranger.request.post(endpoint + '/guests', data={'name': 'Protected guest'}).status == 401
        wrong = stranger.request.post(endpoint + '/guests', data={'name': 'Protected guest', 'password': 'Wrong-Access-123'})
        assert wrong.status == 401 and wrong.json()['detail'] == 'The session password is incorrect.'
        invite.locator('[name=name]').fill('Protected guest')
        invite.locator('[name=password]').fill('Guest-Access-123')
        invite.locator('button[type=submit]').click()
        invite.wait_for_function("document.querySelector('#group-solve')?.dataset.revision !== undefined")
        identity = stranger.request.get(endpoint).json(); guest_id = identity['user_id']
        assert guest_id < 0 and identity['password_protected']
        token = next(item['value'] for item in stranger.cookies() if item['name'] == 'session')
        with connect(ws_url, origin=base, additional_headers={'Cookie': 'session=' + token}, proxy=None) as socket:
            assert json.loads(socket.recv(timeout=2))['type'] == 'ready'
            rotated = host.request.post(endpoint + '/settings', data={
                'allow_guests': True, 'password': 'Rotated-Guest-456', 'participant_limit': 0,
            })
            assert rotated.ok and rotated.json()['password_protected']
            pushed = json.loads(socket.recv(timeout=3))
            assert pushed['type'] == 'state' and pushed['snapshot']['password_protected']
        assert stranger.request.get(endpoint).ok
        with connect(ws_url, origin=base, additional_headers={'Cookie': 'session=' + token}, proxy=None) as socket:
            assert json.loads(socket.recv(timeout=2))['type'] == 'ready'

        newcomer = browser.new_context()
        assert newcomer.request.post(endpoint + '/guests', data={
            'name': 'New protected guest', 'password': 'Guest-Access-123',
        }).status == 401
        joined = newcomer.request.post(endpoint + '/guests', data={
            'name': 'New protected guest', 'password': 'Rotated-Guest-456',
        })
        assert joined.ok
        conflict = host.request.post(endpoint + '/settings', data={
            'allow_guests': True, 'password': 'Another-Guest-789', 'remove_password': True,
        })
        assert conflict.status == 422
        assert newcomer.request.get(endpoint).json()['password_protected']

        other = host.request.post(create, data={'allow_guests': True, 'password': 'Other-Session-789'}).json()
        other_endpoint = base + '/api/groups/' + other['id']
        assert stranger.request.get(other_endpoint).status == 401
        assert stranger.request.post(other_endpoint + '/guests', data={
            'name': 'Cross session', 'password': 'Rotated-Guest-456',
        }).status == 401

        approval = host.request.post(create, data={
            'allow_guests': True, 'approval_required': True, 'password': 'Approval-Guest-123',
        }).json()
        approval_endpoint = base + '/api/groups/' + approval['id']
        approval_guest = browser.new_context()
        pending = approval_guest.request.post(approval_endpoint + '/guests', data={
            'name': 'Approval guest', 'password': 'Approval-Guest-123',
        }).json()
        assert pending['pending'] and approval_guest.request.get(approval_endpoint).status == 403
        approval_id = next(member['user_id'] for member in host.request.get(approval_endpoint).json()['members']
                           if member['name'] == 'Approval guest')
        assert host.request.post(approval_endpoint + '/members', data={'user_id': approval_id, 'action': 'approve'}).ok
        assert approval_guest.request.get(approval_endpoint).ok

        removed = host.request.post(endpoint + '/settings', data={
            'allow_guests': True, 'remove_password': True, 'participant_limit': 0,
        })
        assert removed.ok and not removed.json()['password_protected']
        with SessionLocal() as db:
            assert db.get(GroupSolve, created['id']).password_hash is None
        unprotected = browser.new_context()
        assert unprotected.request.post(endpoint + '/guests', data={'name': 'No password guest'}).ok

        limited = host.request.post(create, data={'allow_guests': True, 'password': 'Limited-Guest-123'}).json()
        limited_endpoint = base + '/api/groups/' + limited['id']
        attacker = browser.new_context()
        for index in range(5):
            response = attacker.request.post(limited_endpoint + '/guests', data={
                'name': f'Attempt {index}', 'password': 'Wrong-Guest-123',
            })
            assert response.status == 401
        blocked = attacker.request.post(limited_endpoint + '/guests', data={
            'name': 'Blocked', 'password': 'Limited-Guest-123',
        })
        assert blocked.status == 429 and int(blocked.headers['retry-after']) > 0

        host_page = host.new_page(); host_page.goto(base + created['url'])
        host_page.wait_for_function("document.querySelector('#group-solve')?.dataset.revision !== undefined")
        host_page.locator('#group-host > summary').click()
        assert host_page.locator('#group-password-status').inner_text() == 'Session password protection is disabled.'
        assert host_page.locator('#group-settings [name=password]').get_attribute('autocomplete') == 'new-password'
        assert host_page.locator('#group-settings [name=remove_password]').count() == 1
        assert invite.evaluate('document.documentElement.scrollWidth <= innerWidth')

        browser.close()
        print('Group password hashing, prompts, API/WebSocket enforcement, rotation, reconnect, removal, approval, isolation, throttling and mobile UI passed.')
finally:
    server.terminate()
    server.wait(timeout=15)
