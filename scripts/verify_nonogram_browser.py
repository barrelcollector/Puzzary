"""Live opt-in browser smoke test: creates one sample puzzle, resets its test progress.

Run inside the candidate service with QA_OUTPUT set to a mounted artifact directory.
"""
import base64
import json
import os
from pathlib import Path

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import select

from app.database import SessionLocal
from app.main import SESSION_SECRET
from app.models import Puzzle, User

output = Path(os.environ.get('QA_OUTPUT', '/qa'))
output.mkdir(parents=True, exist_ok=True)
base = os.environ.get('QA_BASE', 'http://127.0.0.1:8790')
with SessionLocal() as db:
    admin = db.scalar(select(User).where(User.is_admin == True, User.is_active == True).order_by(User.id))
    player = db.scalar(select(User).where(User.is_admin == False, User.is_active == True).order_by(User.id))
    def cookie(user):
        payload = base64.b64encode(json.dumps({'user_id': user.id, 'session_version': user.session_version or 1}).encode())
        return TimestampSigner(SESSION_SECRET).sign(payload).decode()
    admin_cookie = cookie(admin)
    player_cookie = cookie(player) if player else None

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={'width':1280,'height':1000})
    context.add_cookies([{'name':'session','value':admin_cookie,'url':base}])
    existing_id = os.environ.get('QA_PUZZLE_ID', '')
    response = context.request.get(base+'/api/puzzles/'+existing_id) if existing_id else context.request.post(base+'/api/puzzles/generate', data={
        'puzzle_type':'nonogram','theme':'','difficulty':'medium','seed':'nonogram-browser-test',
        'generation_options':{'grid_size':10,'symmetry':'mirror','gimme_count':2},
    })
    assert response.ok, response.text()
    puzzle_id = response.json()['id']
    (output/'nonogram-id.txt').write_text(puzzle_id)
    with SessionLocal() as db:
        puzzle = db.get(Puzzle, puzzle_id)
        grid = puzzle.solution_payload['grid']
        givens = puzzle.solution_payload['givens']
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(base+'/puzzles/'+puzzle_id)
    page.wait_for_selector('.nonogram-play')
    cells = page.locator('.nonogram-play button.nonogram-cell:not([disabled])')
    first = cells.first
    first.click()
    assert first.get_attribute('data-value') == '1'
    page.wait_for_timeout(700)
    progress = context.request.get(base+f'/api/puzzles/{puzzle_id}/progress')
    assert progress.ok, progress.text()
    assert progress.json()['answers'][first.get_attribute('data-key')] == '1'
    page.reload()
    assert page.locator('.nonogram-play button.nonogram-cell:not([disabled])').first.get_attribute('data-value') == '1'
    page.locator('.nonogram-clear').click()
    page.locator('[data-mode="0"]').click()
    first.click()
    assert first.get_attribute('data-value') == '0'
    first.press('Delete')
    assert first.get_attribute('data-value') == ''
    page.locator('[data-mode="1"]').click()
    # Paint across adjacent cells using a real mouse drag.
    row = next(y for y in range(10) if all(f'{x},{y}' not in givens for x in (0,1,2)))
    start = page.locator(f'.nonogram-play [data-key="0,{row}"]').bounding_box()
    end = page.locator(f'.nonogram-play [data-key="2,{row}"]').bounding_box()
    page.mouse.move(start['x']+start['width']/2,start['y']+start['height']/2)
    page.mouse.down()
    page.mouse.move(end['x']+end['width']/2,end['y']+end['height']/2,steps=12)
    page.mouse.up()
    assert all(page.locator(f'.nonogram-play [data-key="{x},{row}"]').get_attribute('data-value') == '1' for x in (0,1,2))
    page.locator('.nonogram-check').click()
    assert 'incorrect' in page.locator('.nonogram-status').inner_text()
    page.screenshot(path=str(output/'nonogram-desktop.png'),full_page=True)
    page.locator('summary').filter(has_text='Hints').click()
    page.locator('#reveal-solution').click()
    page.wait_for_timeout(700)
    progress = context.request.get(base+f'/api/puzzles/{puzzle_id}/progress').json()
    assert progress['found_count'] == progress['expected_count'] == 100
    if player_cookie:
        player_context = browser.new_context()
        player_context.add_cookies([{'name':'session','value':player_cookie,'url':base}])
        player_page=player_context.new_page()
        player_page.goto(base+'/puzzles/'+puzzle_id)
        assert player_page.get_by_role('heading',name='Admin Solution').count() == 0
        other = player_context.request.get(base+f'/api/puzzles/{puzzle_id}/progress').json()
        assert other['answers'] == {}
        denied=player_context.request.post(base+'/api/puzzles/generate',data={'puzzle_type':'nonogram'})
        assert denied.status == 403
        player_context.close()
    page.locator('.nonogram-clear').click()
    page.wait_for_timeout(700)
    mobile = browser.new_context(viewport={'width':390,'height':844},has_touch=True,is_mobile=True)
    mobile.add_cookies([{'name':'session','value':admin_cookie,'url':base}])
    phone = mobile.new_page()
    phone.goto(base+'/puzzles/'+puzzle_id)
    phone.locator('.nonogram-play button.nonogram-cell:not([disabled])').first.tap()
    assert phone.locator('.nonogram-play button.nonogram-cell:not([disabled])').first.get_attribute('data-value') == '1'
    assert phone.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    phone.screenshot(path=str(output/'nonogram-mobile.png'),full_page=True)
    phone.locator('.nonogram-clear').click()
    phone.wait_for_timeout(700)
    page.goto(base+'/admin/puzzles/manual')
    page.locator('[data-manual-puzzle-type]').select_option('nonogram')
    editor = page.locator('[data-level-editor="nonogram"]')
    editor.wait_for()
    editor.locator('[data-key="2,2"]').click()
    assert json.loads(page.locator('[name="manual_solution_grid"]').input_value())[2][2] == 1
    page.screenshot(path=str(output/'nonogram-editor.png'),full_page=True)
    page.goto(base+'/generate')
    page.locator('[data-puzzle-type]').select_option('nonogram')
    schema = page.locator('#generation-schemas').text_content()
    assert json.loads(schema)['nonogram']['needs_word_bank'] is False
    assert page.locator('[name="gen_grid_size"]').count() == 1
    assert page.locator('[name="gen_symmetry"]').count() == 1
    for size in ('letter','a4','zebra'):
        pdf = context.request.get(base+f'/puzzles/{puzzle_id}/print.pdf?size={size}')
        assert pdf.ok, pdf.text()
        assert pdf.body().startswith(b'%PDF')
        (output/f'nonogram-{size}.pdf').write_bytes(pdf.body())
    assert not errors, errors
    browser.close()
print('Browser checks passed: mouse drag, touch, keyboard, save/reload, checking, reveal, account isolation, admin editor, PDF.')
print('Sample puzzle:', puzzle_id)
