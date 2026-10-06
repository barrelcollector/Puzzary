"""Opt-in live Takuzu generation/play/print/editor QA; clears its test progress."""
import base64
from io import BytesIO
import json
import os
from pathlib import Path

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from pypdf import PdfReader
from sqlalchemy import select

from app.database import SessionLocal
from app.main import SESSION_SECRET
from app.models import Puzzle, User

base='http://127.0.0.1:8790'
output=Path(os.environ.get('QA_OUTPUT','/tmp/takuzu-live')); output.mkdir(parents=True,exist_ok=True)
with SessionLocal() as db:
    admin=db.scalar(select(User).where(User.is_admin==True,User.is_active==True).order_by(User.id))
    player=db.scalar(select(User).where(User.is_admin==False,User.is_active==True).order_by(User.id))
    def cookie(user):
        payload=base64.b64encode(json.dumps({'user_id':user.id,'session_version':user.session_version or 1}).encode())
        return TimestampSigner(SESSION_SECRET).sign(payload).decode()
    admin_cookie=cookie(admin); player_cookie=cookie(player) if player else None

with sync_playwright() as p:
    browser=p.chromium.launch()
    context=browser.new_context(viewport={'width':1280,'height':1000})
    context.add_cookies([{'name':'session','value':admin_cookie,'url':base}])
    existing=os.environ.get('QA_PUZZLE_ID')
    response=context.request.get(base+'/api/puzzles/'+existing) if existing else context.request.post(base+'/api/puzzles/generate',data={
        'puzzle_type':'takuzu','theme':'','difficulty':'medium','seed':'takuzu-live-test',
        'generation_options':{'grid_size':6,'auto_confirm_lines':'yes'}})
    assert response.ok,response.text()
    puzzle_id=response.json()['id']; (output/'takuzu-id.txt').write_text(puzzle_id)
    with SessionLocal() as db:
        puzzle=db.get(Puzzle,puzzle_id); grid=puzzle.solution_payload['grid']
    page=context.new_page(); errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(base+'/generate')
    page.locator('[data-puzzle-type]').select_option('takuzu')
    assert not json.loads(page.locator('#generation-schemas').text_content())['takuzu']['needs_word_bank']
    assert page.locator('[name="gen_grid_size"]').count()==1
    page.goto(base+'/puzzles/'+puzzle_id)
    assert page.get_by_role('heading',name='Admin Solution').is_visible()
    assert page.locator('.side .takuzu-cell').count()==36
    page.locator('.takuzu-clear').click(); page.wait_for_timeout(600)
    first=page.locator('.takuzu-play button.takuzu-cell:not([disabled])').first
    key=first.get_attribute('data-key')
    first.click(); assert first.inner_text()=='0'
    page.wait_for_timeout(600)
    page.reload(); assert first.inner_text()=='0'
    progress=context.request.get(base+f'/api/puzzles/{puzzle_id}/progress').json()
    assert progress['answers'][key]=='0'
    page.screenshot(path=str(output/'takuzu-desktop.png'),full_page=True)
    page.locator('.takuzu-clear').click()
    for y,row in enumerate(grid):
        for x,value in enumerate(row):
            cell=page.locator(f'.takuzu-play [data-key="{x},{y}"]')
            if cell.is_enabled(): cell.press(str(value))
    assert page.locator('.takuzu-status').inner_text()=='Takuzu complete.'
    assert page.locator('#completion-celebration').is_visible()
    page.wait_for_timeout(600)
    progress=context.request.get(base+f'/api/puzzles/{puzzle_id}/progress').json()
    assert progress['found_count']==progress['expected_count']==36
    if player_cookie:
        other=browser.new_context()
        other.add_cookies([{'name':'session','value':player_cookie,'url':base}])
        player_page=other.new_page(); player_page.goto(base+'/puzzles/'+puzzle_id)
        assert player_page.get_by_role('heading',name='Admin Solution').count()==0
        assert other.request.get(base+f'/api/puzzles/{puzzle_id}/progress').json()['answers']=={}
        assert other.request.post(base+'/api/puzzles/generate',data={'puzzle_type':'takuzu'}).status==403
        other.close()
    page.locator('.takuzu-clear').click(); page.wait_for_timeout(600)
    phone=browser.new_context(viewport={'width':390,'height':844},has_touch=True,is_mobile=True)
    phone.add_cookies([{'name':'session','value':admin_cookie,'url':base}])
    mobile=phone.new_page(); mobile.goto(base+'/puzzles/'+puzzle_id)
    cell=mobile.locator('.takuzu-play button.takuzu-cell:not([disabled])').first
    cell.tap(); assert cell.inner_text()=='0'
    assert mobile.evaluate('document.documentElement.scrollWidth <= innerWidth')
    box=cell.bounding_box(); assert abs(box['width']-box['height'])<1
    mobile.screenshot(path=str(output/'takuzu-mobile.png'),full_page=True)
    mobile.locator('.takuzu-clear').click(); mobile.wait_for_timeout(600)
    phone.close()
    page.goto(base+'/admin/puzzles/manual')
    page.locator('[data-manual-puzzle-type]').select_option('takuzu')
    editor=page.locator('[data-level-editor="takuzu"]'); editor.wait_for()
    assert 'Valid' in editor.locator('[role="status"]').inner_text()
    editor.locator('[data-key="0,0"]').click()
    assert json.loads(page.locator('[name="manual_solution_grid"]').input_value())[0][0]==1
    page.screenshot(path=str(output/'takuzu-editor.png'),full_page=True)
    for size in ('letter','a4','zebra'):
        pdf=context.request.get(base+f'/puzzles/{puzzle_id}/print.pdf?size={size}')
        assert pdf.ok,pdf.text()
        assert len(PdfReader(BytesIO(pdf.body())).pages)==1
        (output/f'takuzu-{size}.pdf').write_bytes(pdf.body())
    assert not errors,errors
    browser.close()
print('Live generation, account progress, auto-completion, touch, admin solution/editor and PDFs passed.')
print('Sample puzzle:',puzzle_id)
