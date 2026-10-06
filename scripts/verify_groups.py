"""Isolated API and two-browser Group Solve QA; never uses the live database."""
import os
os.environ['DATABASE_URL'] = 'sqlite:////tmp/puzzary-group-qa.sqlite'
os.environ['SESSION_SECRET'] = 'isolated-group-test-secret'
os.environ['UPLOAD_DIR'] = '/tmp/group-qa-uploads'
import base64
import json
from pathlib import Path
import subprocess
import time
import uuid

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from app.database import Base, engine, SessionLocal
from app.models import GroupSolve, Puzzle, PuzzleProgress, User
from app.puzzles import get_puzzle_type
from app.main import SESSION_SECRET, hash_secret, hash_password

Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
grid = [[0,0,1,1],[0,1,0,1],[1,0,1,0],[1,1,0,0]]
with SessionLocal() as db:
    users = [User(display_name=name, is_admin=i==0, is_active=True, session_version=1, password_hash=hash_password('isolated-test-password'))
             for i,name in enumerate(['QA host','QA player','QA outsider','QA pending'])]
    db.add_all(users); db.flush(); ids=[u.id for u in users]
    users[1].api_token_hash=hash_secret('test-token-not-a-real-secret')
    plugin=get_puzzle_type('takuzu')
    generated=plugin.manual_builder({'solution_grid':grid,'given_count':0,'seed':'groups-qa'})
    puzzle=Puzzle(id='group-qa',puzzle_type='takuzu',title='Group QA',theme='',difficulty='easy',
                  prompt=generated.prompt,play_payload=generated.play_payload,print_payload=generated.print_payload,
                  solution_payload=generated.solution_payload,puzzle_metadata={},source='test')
    db.add(puzzle); db.add(PuzzleProgress(puzzle_id=puzzle.id,user_id=ids[0],progress_payload={'answers':{'personal':'kept'}})); db.commit()
    for kind, options in [('sudoku',{'grid_size':4,'given_count':6}), ('nonogram',{'grid_size':5}), ('cross_equations',{'grid_size':3,'operators':['add']})]:
        module=get_puzzle_type(kind); item=module.generator('', 'easy', None, 'groups-qa', options)
        db.add(Puzzle(id='qa-'+kind,puzzle_type=kind,title='QA '+kind,theme='',difficulty='easy',prompt=item.prompt,
                      play_payload=item.play_payload,print_payload=item.print_payload,solution_payload=item.solution_payload,source='test',puzzle_metadata={}))
    item=get_puzzle_type('crossword').manual_builder({'entries':'CAT|A pet|A feline|Definition: small feline\nCAR|Road transport|Four wheels\nART|Creative work|A painting','seed':'groups-qa','grid_size':'8','gimme_count':1,'gimme_mode':'letters','check_mode':'per_word'})
    db.add(Puzzle(id='qa-crossword',puzzle_type='crossword',title='QA crossword',theme='',difficulty='easy',prompt=item.prompt,
                  play_payload=item.play_payload,print_payload=item.print_payload,solution_payload=item.solution_payload,source='test',puzzle_metadata={}))
    db.commit()

def cookie(user_id):
    return TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({'user_id':user_id,'session_version':1}).encode())).decode()

base='http://127.0.0.1:8791'
server=subprocess.Popen(['uvicorn','app.main:app','--host','127.0.0.1','--port','8791'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
try:
    with sync_playwright() as p:
        browser=p.chromium.launch()
        contexts=[]
        for user_id in ids:
            context=browser.new_context(viewport={'width':1280,'height':1000})
            context.add_cookies([{'name':'session','value':cookie(user_id),'url':base}]); contexts.append(context)
        host,player,outsider,pending=contexts
        for _ in range(80):
            try:
                if host.request.get(base+'/health').ok: break
            except Exception: time.sleep(.1)
        else: raise RuntimeError('QA server failed to start')
        anonymous=browser.new_context()
        assert anonymous.request.post(base+'/api/puzzles/group-qa/groups',data={}).status==401
        response=host.request.post(base+'/api/puzzles/group-qa/groups',data={})
        assert response.ok,response.text()
        data=response.json(); gid=data['id']; code=data['code']
        url=base+data['url']; endpoint=base+'/api/groups/'+gid
        assert outsider.request.get(endpoint).status==403
        assert player.request.post(base+'/api/groups/join',data={'code':code}).ok
        assert outsider.request.post(endpoint+'/close',data={}).status==403
        assert player.request.post(endpoint+'/settings',data={}).status==403
        token_context=browser.new_context(extra_http_headers={'Authorization':'Bearer test-token-not-a-real-secret'})
        assert token_context.request.get(endpoint).ok
        givens=generated.solution_payload['givens']; editable=[f'{x},{y}' for y in range(4) for x in range(4) if f'{x},{y}' not in givens]
        def action(context, revision, changes=None, kind='patch', action_id=None):
            return context.request.post(endpoint+'/actions',data={'action_id':action_id or str(uuid.uuid4()),'revision':revision,'kind':kind,'changes':changes or {}})
        k1,k2=editable[:2]
        action_id=str(uuid.uuid4())
        assert action(host,0,{k1:'0'},action_id=action_id).ok
        assert action(host,0,{k1:'0'},action_id=action_id).json()['revision']==1
        assert action(player,0,{k2:'1'}).ok, 'Disjoint stale edits should merge'
        assert action(player,0,{k1:'1'}).status==409
        latest=host.request.get(endpoint).json(); rev=latest['revision']
        assert latest['state']['answers']=={k1:'0',k2:'1'}
        assert action(host,rev,{'999,999':'1'}).status==422
        assert action(host,rev,{k1:'9'}).status==422
        if givens: assert action(host,rev,{next(iter(givens)):'0'}).status==422
        assert action(host,rev,{},kind='check').ok
        rev=host.request.get(endpoint).json()['revision']
        assert action(host,rev,{k1:'',k2:''}).ok
        # Real UI, existing module handlers, poll updates, reload and independent personal state.
        a=host.new_page(); b=player.new_page(); errors=[]
        for page in [a,b]: page.on('pageerror',lambda error:errors.append(str(error))); page.goto(url)
        a.wait_for_function("!document.querySelector('.puzzle-layout > .panel').inert")
        b.wait_for_function("!document.querySelector('.puzzle-layout > .panel').inert")
        a.locator(f'.takuzu-play [data-key="{k1}"]').press('1')
        b.wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="1"',arg=k1)
        b.locator(f'.takuzu-play [data-key="{k2}"]').press('0')
        a.wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="0"',arg=k2)
        b.reload(); b.wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="1"',arg=k1)
        assert b.locator('#group-members').inner_text().count('online')==2
        assert b.locator('#group-host').is_hidden()
        host.set_offline(True)
        a.locator(f'.takuzu-play [data-key="{k1}"]').press('0'); a.wait_for_timeout(600)
        assert a.locator(f'.takuzu-play [data-key="{k1}"]').inner_text()=='0'
        host.set_offline(False)
        b.wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="0"',arg=k1)
        host.set_offline(True)
        a.locator(f'.takuzu-play [data-key="{k1}"]').press('1'); a.wait_for_timeout(400)
        b.locator(f'.takuzu-play [data-key="{k1}"]').press('1'); b.wait_for_timeout(700)
        host.set_offline(False)
        a.wait_for_function("document.querySelector('#group-status').textContent.includes('Another player changed')")
        a.wait_for_timeout(1500)
        assert 'Another player changed' in a.locator('#group-status').inner_text(), 'Conflict warning must survive polling'
        a.locator(f'.takuzu-play [data-key="{k2}"]').press('1')
        a.wait_for_function("!document.querySelector('#group-status').textContent.includes('Another player changed')")
        a.locator('#group-check').click(); a.wait_for_timeout(500)
        a.locator('#group-hint').click(); a.wait_for_timeout(500)
        assert host.request.get(endpoint).json()['state']['hints_used']==1
        out=Path('/tmp/groups-qa'); out.mkdir(exist_ok=True)
        a.screenshot(path=str(out/'group-desktop.png'),full_page=True)
        b.set_viewport_size({'width':390,'height':844}); b.screenshot(path=str(out/'group-mobile.png'),full_page=True)
        assert b.evaluate('document.documentElement.scrollWidth <= innerWidth')
        b.locator('#group-leave').click(); b.wait_for_url('**/puzzles/group-qa')
        assert player.request.get(endpoint).status==403
        assert player.request.post(base+'/api/groups/join',data={'code':code}).ok
        assert host.request.post(endpoint+'/settings',data={'participant_limit':2,'approval_required':True}).ok
        assert pending.request.post(base+'/api/groups/join',data={'code':code}).status==409
        assert host.request.post(endpoint+'/settings',data={'participant_limit':0,'approval_required':True}).ok
        assert pending.request.post(base+'/api/groups/join',data={'code':code}).json()['pending']
        assert pending.request.get(endpoint).status==403
        assert pending.request.post(endpoint+'/actions',data={'action_id':str(uuid.uuid4()),'revision':0}).status==403
        assert player.request.post(endpoint+'/members',data={'user_id':ids[3],'action':'approve'}).status==403
        assert host.request.post(endpoint+'/members',data={'user_id':ids[3],'action':'approve'}).ok
        assert pending.request.get(endpoint).ok
        assert host.request.post(endpoint+'/members',data={'user_id':ids[3],'action':'remove'}).ok
        assert pending.request.get(endpoint).status==403
        assert pending.request.post(base+'/api/groups/join',data={'code':code}).status==403
        latest=host.request.get(endpoint).json()
        solved={key:str(grid[int(key.split(',')[1])][int(key.split(',')[0])]) for key in editable}
        assert action(player,latest['revision'],solved).json()['completed']
        a.wait_for_function("document.querySelector('#group-status').textContent==='Group puzzle complete.'")
        assert a.locator('#completion-celebration').is_visible()
        with SessionLocal() as db:
            personal=db.query(PuzzleProgress).filter_by(puzzle_id='group-qa',user_id=ids[0]).one()
            assert personal.progress_payload=={'answers':{'personal':'kept'}}
            assert db.query(PuzzleProgress).filter_by(puzzle_id='group-qa',user_id=ids[1]).count()==0
        assert host.request.post(endpoint+'/close',data={}).ok
        assert action(player,host.request.get(endpoint).json()['revision'],{k1:''}).status==409
        a.reload(); a.wait_for_function("document.querySelector('#group-status').textContent.startsWith('Session closed')")
        assert a.locator('.puzzle-layout > .panel').evaluate('(e)=>e.inert')
        for kind in ['sudoku','crossword','cross_equations','nonogram']:
            info=host.request.post(base+f'/api/puzzles/qa-{kind}/groups',data={}).json()
            page1=host.new_page(); page2=player.new_page()
            for page in [page1,page2]:
                page.on('pageerror',lambda error:errors.append(str(error))); page.goto(base+info['url'])
                page.wait_for_function("!document.querySelector('.puzzle-layout > .panel').inert")
            editable_cell=page1.locator('.puzzle-layout input[data-key]:not([disabled])').first if kind!='nonogram' else page1.locator('.nonogram-play button[data-key]:not([disabled])').first
            key=editable_cell.get_attribute('data-key')
            value='A' if kind=='crossword' else '1'
            editable_cell.press(' ' if kind=='nonogram' else value)
            if kind=='nonogram':
                page2.wait_for_function('(key)=>document.querySelector(`.nonogram-play button[data-key="${key}"]`).dataset.value==="1"',arg=key)
            else:
                page2.wait_for_function('([key,value])=>document.querySelector(`.puzzle-layout input[data-key="${key}"]`).value===value',arg=[key,value])
            endpoint2=base+'/api/groups/'+info['id']
            assert host.request.get(endpoint2).json()['state']['answers'][key]==value
            if kind=='crossword':
                more=page1.locator('.more-clue').first
                if more.count():
                    more.click()
                    page2.wait_for_function("[...document.querySelectorAll('.clue-steps')].some(e=>!e.hidden)")
                page1.locator('.reveal-entry:not([disabled])').first.click(); page1.wait_for_timeout(500)
                assert host.request.get(endpoint2).json()['state']['hints_used']>=1
            page1.locator('#group-check').click(); page1.wait_for_timeout(400)
            page1.get_by_text('Hints',exact=True).click(); page1.locator('#reveal-solution').click()
            page2.wait_for_function("document.querySelector('#group-status').textContent==='Group puzzle complete.'")
            assert host.request.get(endpoint2).json()['completed']
            page1.close(); page2.close()
        assert not errors,errors
        browser.close()
    print('Group API/auth/conflict/idempotency/approval/limits/removal/persistence/isolation and two-browser desktop/mobile tests passed.')
finally:
    server.terminate(); server.wait(timeout=10)
