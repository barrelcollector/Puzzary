"""Guest browser/API regression checks, invoked against isolated Group Solve QA."""
import json
from pathlib import Path
import tempfile
import time
import uuid

from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed


def verify_guests(browser, base, host, player, host_id):
    create = base + '/api/puzzles/group-qa/groups'
    assert player.request.post(create, data={'allow_guests': True}).status == 403
    info = host.request.post(create, data={}).json()
    endpoint = base + '/api/groups/' + info['id']
    guest = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    stranger = browser.new_context()
    page = guest.new_page()
    assert guest.request.post(endpoint + '/guests', data={'name': 'QA guest'}).status == 403
    assert player.request.post(endpoint + '/settings', data={'allow_guests': True}).status == 403
    hp = host.new_page()
    hp.goto(base + info['url'])
    hp.wait_for_function("document.querySelector('#group-solve').dataset.revision==='0'")
    hp.locator('#group-host > summary').click()
    hp.locator('#group-settings [name=allow_guests]').check()
    hp.locator('#group-settings button[type=submit]').click()
    for _ in range(50):
        if host.request.get(endpoint).json()['allow_guests']:
            break
        time.sleep(.1)
    else:
        raise AssertionError(hp.locator('#group-message').inner_text())
    page.goto(base + info['url'])
    page.locator('#guest-join [name=name]').fill('QA guest')
    page.locator('#guest-join button').click()
    page.wait_for_function("document.querySelector('#group-solve')?.dataset.revision==='0'")
    identity = guest.request.get(endpoint).json()
    guest_id = identity['user_id']
    assert guest_id < 0 and identity['is_guest'] and not identity['can_allow_guests']
    assert len(identity['members']) == 2
    assert page.locator('#group-host').is_hidden()
    assert page.locator('.nav-link').count() == 0
    assert page.get_by_text('Admin Solution', exact=True).count() == 0
    assert page.locator('.history').count() == 0
    assert guest.request.get(base + '/api/puzzles/group-qa').status == 401
    for route, payload in [(create, {}), (endpoint + '/settings', {'allow_guests': True}),
                           (endpoint + '/close', {}), (endpoint + '/members', {'user_id': host_id, 'action': 'remove'})]:
        response = guest.request.post(route, data=payload)
        assert response.status == 401, (route, response.status)
    for route in ['/library', '/admin', '/account/password', '/puzzles/group-qa']:
        response = guest.request.get(base + route, max_redirects=0)
        assert response.status in (302, 303, 401, 403), (route, response.status)
    assert stranger.request.get(endpoint).status == 401
    other = host.request.post(create, data={'allow_guests': True}).json()
    other_endpoint = base + '/api/groups/' + other['id']
    assert guest.request.get(other_endpoint).status == 401
    assert guest.request.post(base + '/api/groups/join', data={'code': other['id']}).status == 401
    assert stranger.request.post(endpoint + '/guests', data={'name': 'QA guest'}).status == 409
    assert host.request.post(endpoint + '/settings', data={'allow_guests': True, 'participant_limit': 2}).ok
    assert stranger.request.post(endpoint + '/guests', data={'name': 'Another guest'}).status == 409
    page.reload()
    page.wait_for_function("document.querySelector('#group-solve')?.dataset.revision==='0'")
    assert guest.request.get(endpoint).json()['user_id'] == guest_id
    page.wait_for_function('document.querySelector(".group-presence-layer").dataset.transport==="websocket"')
    page.locator('.takuzu-play [data-key="0,0"]').click()
    hp.wait_for_function("document.querySelector('.takuzu-play [data-key=\"0,0\"]').dataset.value==='0'")
    data = host.request.get(endpoint).json()
    assert data['recent'][0]['user_id'] == guest_id and data['recent'][0]['name'] == 'QA guest'
    page.locator('.takuzu-play [data-key="0,0"]').hover()
    hp.wait_for_function('(id)=>!!document.querySelector(`.group-player-pointer[data-user="${id}"]:not([hidden])`)', arg=guest_id)
    output = Path(tempfile.gettempdir())
    page.screenshot(path=str(output / 'group-guest-mobile.png'), full_page=True)
    hp.screenshot(path=str(output / 'group-guest-host.png'), full_page=True)
    token = next(c['value'] for c in guest.cookies() if c['name'] == 'session')
    ws_url = endpoint.replace('http:', 'ws:') + '/presence/ws'
    with connect(ws_url, origin=base, additional_headers={'Cookie': 'session=' + token}, proxy=None) as socket:
        assert json.loads(socket.recv(timeout=2))['type'] == 'ready'
        assert host.request.post(endpoint + '/settings', data={'allow_guests': False}).ok
        assert guest.request.get(endpoint).status == 401
        assert guest.request.post(endpoint + '/actions', data={'revision': data['revision'], 'action_id': str(uuid.uuid4()), 'changes': {'0,1': '1'}}).status == 401
        try:
            socket.recv(timeout=3)
            raise AssertionError('Disabled guest socket remained connected')
        except ConnectionClosed as error:
            assert error.code == 1008
    assert host.request.post(endpoint + '/settings', data={'allow_guests': True, 'approval_required': True}).ok
    result = guest.request.post(base + '/api/groups/join', data={'code': info['id']}).json()
    assert result['pending']
    assert guest.request.get(endpoint).status == 403
    assert host.request.post(endpoint + '/members', data={'user_id': guest_id, 'action': 'approve'}).ok
    assert guest.request.get(endpoint).ok
    assert host.request.post(endpoint + '/members', data={'user_id': guest_id, 'action': 'remove'}).ok
    assert guest.request.post(endpoint + '/guests', data={'name': 'Fresh identity'}).status == 403
    assert guest.request.post(base + '/api/groups/join', data={'code': info['id']}).status == 403
    # Non-admin hosts cannot opt in even for their own sessions.
    own = player.request.post(create, data={}).json()
    assert player.request.post(base + '/api/groups/' + own['id'] + '/settings', data={'allow_guests': True}).status == 403
    hp.close(); page.close(); guest.close(); stranger.close()
    print('Guest opt-in/admin restriction/mobile join/reload/live edits/cursors/isolation/limits/approval/removal/revocation passed.')
