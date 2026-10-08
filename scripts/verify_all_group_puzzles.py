"""Disposable SQLite + two-browser QA for every plugin and the three maze modes."""
import os
os.environ['DATABASE_URL'] = 'sqlite:////tmp/puzzary-all-groups-qa.sqlite'
os.environ['SESSION_SECRET'] = 'isolated-all-groups-test-secret'
os.environ['UPLOAD_DIR'] = '/tmp/all-groups-qa-uploads'
import base64
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from app.database import Base, engine, SessionLocal
from app.models import Puzzle, User, PuzzleProgress
from app.puzzles import get_puzzle_type, list_puzzle_types
from app.main import SESSION_SECRET, hash_password

Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
generated = {}
for plugin in list_puzzle_types():
    assert plugin.collaboration, plugin.slug
    options = {'grid_size': 4} if plugin.slug in ('sudoku', 'takuzu') else {}
    if plugin.slug == 'cross_equations': options = {'grid_size': 3, 'operators': ['add']}
    if plugin.slug == 'maze': options = {'maze_size': 'custom', 'custom_width': 5, 'custom_height': 5, 'allow_loops': 'no'}
    if plugin.slug == 'word_search':
        item = plugin.manual_builder({'grid': 'CATXXXXX\nDOGXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX', 'words': 'CAT,DOG', 'auto_confirm': 'no'})
    elif plugin.slug == 'word_pyramid':
        item = plugin.manual_builder({'rows': 'TIP|Advice|Useful suggestion\nPINT|A measure|Liquid measure\nPOINT|Sharp end|A location', 'gimme_count': 0})
    elif plugin.slug == 'spell_it':
        item = plugin.manual_builder({'entries': [{'answer': 'CAT', 'definition': 'A small feline'}, {'answer': 'DOG', 'definition': 'A canine'}], 'mask_percent': 100, 'preserve_edges': 'no', 'letter_bank': 'off', 'hint_limit': 2})
    else:
        item = plugin.generator('', 'easy', None, 'all-groups-qa', options)
    generated[plugin.slug] = item
with SessionLocal() as db:
    users = [User(display_name=name, is_admin=index == 0, is_active=True, session_version=1, password_hash=hash_password('isolated-test-password')) for index, name in enumerate(['QA Host', 'QA Player'])]
    db.add_all(users); db.flush(); ids = [u.id for u in users]
    for kind, item in generated.items():
        db.add(Puzzle(id='all-'+kind, puzzle_type=kind, title='QA '+kind, theme='', difficulty='easy', prompt=item.prompt,
                      play_payload=item.play_payload, print_payload=item.print_payload, solution_payload=item.solution_payload, puzzle_metadata={}, source='test'))
    second_maze = get_puzzle_type('maze').generator('', 'easy', None, 'second-group-maze', {'maze_size':'custom','custom_width':5,'custom_height':5,'allow_loops':'no'})
    db.add(Puzzle(id='all-maze-second', puzzle_type='maze', title='QA Second Maze', theme='', difficulty='easy', prompt=second_maze.prompt,
                  play_payload=second_maze.play_payload, print_payload=second_maze.print_payload, solution_payload=second_maze.solution_payload, puzzle_metadata={}, source='test'))
    db.commit()

def cookie(uid):
    return TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({'user_id': uid, 'session_version': 1}).encode())).decode()

base = 'http://127.0.0.1:8792'
artifacts = Path(os.environ.get('QA_ARTIFACT_DIR', Path(tempfile.gettempdir()) / 'puzzary-qa-artifacts'))
artifacts.mkdir(parents=True, exist_ok=True)
server = subprocess.Popen(['uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8792'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        contexts = [browser.new_context(viewport={'width':1280, 'height':900}) for _ in ids]
        for context, uid in zip(contexts, ids): context.add_cookies([{'name':'session', 'value':cookie(uid), 'url':base}])
        host, player = contexts
        for _ in range(100):
            try:
                if host.request.get(base+'/health').ok: break
            except Exception: time.sleep(.1)
        else: raise RuntimeError('QA server did not start')
        groups = {}
        for kind in generated:
            response = host.request.post(base+f'/api/puzzles/all-{kind}/groups', data={})
            assert response.ok, (kind, response.text())
            groups[kind] = response.json()
        print('All nine modules expose Group Solve and create sessions.')
        pages = [host.new_page(), player.new_page()]
        errors = []
        for page in pages: page.on('pageerror', lambda error: errors.append(str(error)))

        def open_group(kind):
            info = groups[kind]
            endpoint = base+'/api/groups/'+info['id']
            response = player.request.post(base+'/api/groups/join', data={'code':info['code']})
            assert response.ok, response.text()
            for page in pages:
                page.goto(base+info['url'])
                page.wait_for_function('document.querySelector("#group-solve").dataset.revision !== undefined')
                page.wait_for_function('!document.querySelector(".puzzle-layout > .panel").inert')
            return endpoint

        def act(endpoint, context, changes=None, kind='patch', target='', status=200, revision=None):
            current = host.request.get(endpoint).json()
            response = context.request.post(endpoint+'/actions', data={'action_id':str(uuid.uuid4()), 'revision':current['revision'] if revision is None else revision, 'puzzle_id':current['puzzle_id'], 'kind':kind, 'target':target, 'changes':changes or {}})
            assert response.status == status, (status, response.status, response.text())
            return response.json()

        endpoint = open_group('word_pyramid')
        a, b = pages
        a.locator('input[data-row="0"][data-cell="2"]').fill('P')
        b.wait_for_function('document.querySelector(`input[data-row="0"][data-cell="2"]`).value === "P"')
        assert b.locator('input[data-row="0"][data-cell="0"]').input_value() == ''
        b.locator('input[data-row="0"][data-cell="0"]').fill('T')
        a.wait_for_function('document.querySelector(`input[data-row="0"][data-cell="0"]`).value === "T"')
        a.locator('input[data-row="0"][data-cell="1"]').fill('I')
        b.wait_for_function('document.querySelector(`input[data-row="0"][data-cell="1"]`).value === "I"')
        a.locator('.more-pyramid-clue').first.click()
        b.wait_for_function('!document.querySelector(".pyramid-clue-steps").hidden')
        a.locator('.reveal-pyramid-row[data-row="1"]').click()
        b.wait_for_function('document.querySelector(`input[data-row="1"][data-cell="0"]`).value === "P"')
        act(endpoint, host, {'0':'X'*90}, status=422)
        print('Pyramid positional gaps, edits, row reveal and clues synchronize.')

        endpoint = open_group('word_search')
        def select(page, x1, y1, x2, y2):
            first = page.locator(f'button[data-x="{x1}"][data-y="{y1}"]').bounding_box()
            last = page.locator(f'button[data-x="{x2}"][data-y="{y2}"]').bounding_box()
            page.mouse.move(first['x']+first['width']/2, first['y']+first['height']/2)
            page.mouse.down(); page.mouse.move(last['x']+last['width']/2, last['y']+last['height']/2, steps=8); page.mouse.up()
        select(a, 0, 0, 2, 0)
        b.wait_for_function('document.querySelectorAll(".candidate-cell").length === 3')
        assert 'CAT' not in host.request.get(endpoint).json()['state'].get('found_words', [])
        b.locator('#word-search-check').click()
        a.wait_for_function('document.querySelector("[data-word=CAT]").classList.contains("found")')
        select(a, 4, 2, 6, 2)
        b.wait_for_function('document.querySelectorAll(".candidate-cell").length === 3')
        b.locator('#group-check').click()
        a.wait_for_function('document.querySelectorAll(".miss").length === 3')
        select(a, 4, 2, 6, 2)
        b.wait_for_function('document.querySelectorAll(".miss").length === 0')
        act(endpoint, host, {'guess:99,0:100,0':'pending'}, status=422)
        print('Word Search shares unchecked/wrong selections, checks and deselection.')

        endpoint = open_group('spell_it')
        a.locator('input[data-position="0"]').fill('X')
        b.wait_for_function('document.querySelector(`input[data-position="0"]`).value === "X"')
        focused = b.locator('input[data-position="1"]'); focused.focus()
        act(endpoint, host, {'0:2':'T'})
        b.wait_for_function('document.querySelector(`input[data-position="2"]`).value === "T"')
        assert focused.evaluate('(el)=>el === document.activeElement')
        a.get_by_role('button', name='Reveal Letter (2)', exact=True).click()
        b.wait_for_function('document.querySelector(`input[data-position="0"]`).readOnly')
        assert b.locator('input[data-position="0"]').input_value() == 'C'
        act(endpoint, host, {'hint:0':'0,1'}, status=422)
        act(endpoint, player, {'0:0':'X'}, status=422)
        b.locator('input[data-position="1"]').fill('A')
        a.wait_for_function('document.querySelector(`input[data-position="1"]`).value === "A"')
        a.get_by_role('button', name='Check Word', exact=True).click()
        b.wait_for_function('document.querySelector(".spell-status").textContent === "Correct."')
        a.get_by_role('button', name='Next Word', exact=True).click()
        assert a.locator('.spell-definition').inner_text() == 'A canine'
        print('Spell It shares edits, hints/budgets, checking and progression without losing focus.')

        endpoint = open_group('maze')
        route = generated['maze'].solution_payload['path']
        def draw_path(page, points):
            page.locator('.maze-play .maze-board').scroll_into_view_if_needed()
            box = page.locator('.maze-play .maze-board').bounding_box()
            width = generated['maze'].play_payload['settings']['width']
            height = generated['maze'].play_payload['settings']['height']
            for index, point in enumerate(points):
                page.mouse.move(box['x']+(point['x']+.5)*box['width']/width, box['y']+(point['y']+.5)*box['height']/height)
                if index == 0: page.mouse.down()
            page.mouse.up()
        draw_path(a, route[:3])
        b.wait_for_function('document.querySelector(".maze-trace-path").getAttribute("points").split(" ").length === 3')
        act(endpoint, player, {'path':json.dumps([route[0],route[-1]])}, status=422)
        a.reload(); a.wait_for_function('document.querySelector(".maze-trace-path").getAttribute("points").split(" ").length === 3')
        before = host.request.get(endpoint).json()['revision']
        assert player.request.post(endpoint+'/settings', data={'puzzle_options':{'maze_mode':'companions'}}).status == 403
        response = host.request.post(endpoint+'/settings', data={'puzzle_options':{'maze_mode':'companions'}})
        assert response.ok, response.text()
        a.wait_for_function('document.querySelectorAll(".maze-player-legend span").length === 2')
        b.wait_for_function('document.querySelectorAll(".maze-player-legend span").length === 2')
        act(endpoint, host, {f'path:{ids[0]}':json.dumps(route[:2])}, revision=before, status=409)
        act(endpoint, player, {f'path:{ids[0]}':json.dumps(route[:2])}, status=422)
        version = host.request.get(endpoint).json()['revision']
        act(endpoint, host, {f'path:{ids[0]}':json.dumps(route[:3])}, revision=version)
        act(endpoint, player, {f'path:{ids[1]}':json.dumps(route[:4])}, revision=version)
        b.wait_for_function('document.querySelectorAll(".maze-board polyline[points]").length >= 2')
        a.set_viewport_size({'width':390,'height':844})
        a.screenshot(path=str(artifacts / 'all-groups-maze-companions-mobile.png'), full_page=True)
        assert a.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile overflow'
        a.set_viewport_size({'width':1280,'height':900})
        a.screenshot(path=str(artifacts / 'all-groups-maze-companions-desktop.png'), full_page=True)
        act(endpoint, host, {f'path:{ids[0]}':json.dumps(route)})
        data = act(endpoint, player, {f'path:{ids[1]}':json.dumps(route)})
        assert data['completed'] and data['expected_count'] == 2
        response = host.request.post(endpoint+'/settings', data={'puzzle_options':{'maze_mode':'rally', 'rally_legs':'4'}})
        assert response.ok, response.text()
        checkpoints = response.json()['state']['maze_group']['checkpoints']
        a.wait_for_function('document.querySelectorAll(".maze-board circle").length === 5')
        b.wait_for_function('document.querySelectorAll(".maze-board circle").length === 5')
        act(endpoint, player, {'leg:0':json.dumps(route[:2])}, status=422)
        act(endpoint, host, {'leg:1':json.dumps(route[:2])}, status=422)
        for leg in range(len(checkpoints)-1):
            start = route.index(checkpoints[leg]); finish = route.index(checkpoints[leg+1])
            data = act(endpoint, contexts[leg%2], {f'leg:{leg}':json.dumps(route[start:finish+1])})
            assert data['found_count'] == leg+1
        assert data['completed']
        b.wait_for_function('document.querySelector("#maze-status").textContent === "Rally complete."')
        a.screenshot(path=str(artifacts / 'all-groups-maze-rally-desktop.png'), full_page=True)
        # Move through the real host UI, change legs, then play without reloading.
        a.locator('#group-host').evaluate('(el)=>el.open=true')
        a.locator('#group-puzzle-picker').evaluate('(el)=>el.open=true')
        a.wait_for_function('!!document.querySelector("#group-move option[value=all-maze-second]")')
        a.locator('#group-move select').select_option('all-maze-second')
        a.locator('#group-move button').click()
        for page in pages:
            page.wait_for_url('**/puzzles/all-maze-second?group=*')
            page.wait_for_function('!document.querySelector(".puzzle-layout > .panel").inert')
            page.evaluate('''() => {
                window.progressChimes = 0;
                window.PuzzarySounds.progress = () => { window.progressChimes += 1; };
                window.mazeNode = document.querySelector('.maze-play');
                const original = window.fetch;
                window.fetch = async (...args) => {
                    const response = await original(...args);
                    if (String(args[0]).endsWith('/actions') && args[1]?.method === 'POST') await new Promise(resolve => setTimeout(resolve,350));
                    return response;
                };
            }''')
        a.locator('#group-host').evaluate('(el)=>el.open=true')
        a.locator('#group-settings [data-group-option=maze_mode]').select_option('rally')
        a.locator('#group-settings [data-group-option=rally_legs]').fill('3')
        a.once('dialog', lambda dialog:dialog.accept())
        a.locator('#group-settings button[type=submit]').click()
        for page in pages: page.wait_for_function('document.querySelectorAll(".maze-turn-order li").length === 3')
        current = host.request.get(endpoint).json()
        checkpoints = current['state']['maze_group']['checkpoints']
        new_route = second_maze.solution_payload['path']
        for leg in range(3):
            active_page = pages[leg%2]
            start = new_route.index(checkpoints[leg]); finish = new_route.index(checkpoints[leg+1])
            draw_path(active_page,new_route[start:finish+1])
            next_leg = leg + 1
            for page in pages:
                page.wait_for_function('(n)=>document.querySelectorAll(".maze-turn-order .leg-complete").length === n',arg=next_leg)
                assert page.evaluate('window.mazeNode === document.querySelector(".maze-play")'), 'Unexpected page refresh'
                assert page.evaluate('window.progressChimes') == min(next_leg,2), 'Chime must play once per non-final leg'
            if next_leg < 3:
                expected_player = ids[next_leg%2]
                for page in pages:
                    page.wait_for_function('(id)=>document.querySelector("#group-members .current-turn")?.dataset.player === String(id)',arg=expected_player)
        # Reset leg count through host controls; both players must immediately resume.
        a.locator('#group-settings [data-group-option=rally_legs]').fill('4')
        a.once('dialog', lambda dialog:dialog.accept())
        a.locator('#group-settings button[type=submit]').click()
        for page in pages: page.wait_for_function('document.querySelectorAll(".maze-turn-order li").length === 4')
        for page in pages: assert page.locator('#completion-celebration').is_hidden(), 'Resetting legs must clear the old completion banner'
        current = host.request.get(endpoint).json()
        checkpoints = current['state']['maze_group']['checkpoints']
        finish = new_route.index(checkpoints[1])
        draw_path(a,new_route[:finish+1])
        for page in pages: page.wait_for_function('document.querySelectorAll(".maze-turn-order .leg-complete").length === 1')
        assert a.evaluate('window.progressChimes') == 3
        a.set_viewport_size({'width':390,'height':844})
        a.screenshot(path=str(artifacts / 'maze-rally-turn-order-mobile.png'),full_page=True)
        assert a.evaluate('document.documentElement.scrollWidth <= innerWidth')
        a.set_viewport_size({'width':1280,'height':900})
        a.screenshot(path=str(artifacts / 'maze-rally-turn-order-desktop.png'),full_page=True)
        # Return to the first maze for the original guest regression checks.
        data = host.request.get(endpoint).json()
        response = host.request.post(endpoint+'/puzzle',data={'puzzle_id':'all-maze','revision':data['revision']})
        assert response.ok,response.text()
        for page in pages: page.wait_for_url('**/puzzles/all-maze?group=*')
        print('Real host move/settings/leg-count reset and delayed browser saves passed without refresh; turn highlighting/order and non-duplicate progress chimes passed.')
        response = host.request.post(endpoint+'/settings', data={'allow_guests':True, 'puzzle_options':{'maze_mode':'co_op'}})
        assert response.ok, response.text()
        guest = browser.new_context(viewport={'width':390, 'height':844}, has_touch=True)
        response = guest.request.post(endpoint+'/guests', data={'name':'QA Maze Guest'})
        assert response.ok, response.text()
        gid = groups['maze']['id']
        guest_id = guest.request.get(endpoint).json()['user_id']
        assert guest_id < 0
        response = host.request.post(endpoint+'/settings', data={'allow_guests':True, 'puzzle_options':{'maze_mode':'companions'}})
        assert response.ok, response.text()
        assert response.json()['expected_count'] == 3
        data = act(endpoint, guest, {f'path:{guest_id}':json.dumps(route)})
        assert data['found_count'] == 1 and not data['completed']
        assert guest.request.post(endpoint+'/settings', data={'puzzle_options':{'maze_mode':'rally'}}).status == 401
        response = host.request.post(endpoint+'/settings', data={'allow_guests':True, 'puzzle_options':{'maze_mode':'rally', 'rally_legs':'3'}})
        assert response.ok, response.text()
        checkpoints = response.json()['state']['maze_group']['checkpoints']
        for leg, context in enumerate([host, player, guest]):
            start = route.index(checkpoints[leg]); finish = route.index(checkpoints[leg+1])
            data = act(endpoint, context, {f'leg:{leg}':json.dumps(route[start:finish+1])})
        assert data['completed'], 'Guest must be able to finish a Rally leg'
        guest_page = guest.new_page()
        guest_page.goto(base+groups['maze']['url'])
        guest_page.wait_for_function('document.querySelector("#maze-status").textContent === "Rally complete."')
        assert guest_page.locator('#group-host').is_hidden()
        assert guest_page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        print('Guest companions and Rally turns, negative participant IDs, host-only settings and mobile reload passed.')
        with SessionLocal() as db:
            assert db.query(PuzzleProgress).count() == 0, 'Group solve must not write personal progress'
        assert not errors, errors
        browser.close()
        print('Maze co-op, independent colored paths, connected checkpoint relay, ownership, stale edits, completion, reload and responsive UI passed.')
finally:
    server.terminate()
    server.wait(timeout=15)
