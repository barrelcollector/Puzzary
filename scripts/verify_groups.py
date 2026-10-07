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
from websockets.sync.client import connect as ws_connect
from websockets.exceptions import InvalidStatus, ConnectionClosed

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from app.database import Base, engine, SessionLocal
from app.models import GroupSolve, GroupMember, Puzzle, PuzzleProgress, User
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
        assert outsider.request.post(endpoint+'/presence',data={}).status==403
        assert player.request.post(base+'/api/groups/join',data={'code':code}).ok
        assert player.request.post(endpoint+'/presence',data={'pointer':{'x':2,'y':0}}).status==422
        assert player.request.post(endpoint+'/presence',data={'entry':['x'*81]}).status==422
        before_presence=host.request.get(endpoint).json()
        response=player.request.post(endpoint+'/presence',data={'pointer':{'x':.25,'y':.5},'focus':'0,0','caret':1,'selection_end':1})
        assert response.ok and 'state' not in response.json()
        assert response.json()['members'][1]['presence']['pointer']=={'key':'','x':.25,'y':.5}
        after_presence=host.request.get(endpoint).json()
        assert before_presence['revision']==after_presence['revision'] and before_presence['state']==after_presence['state']
        assert anonymous.request.post(endpoint+'/presence',data={}).status==401
        with SessionLocal() as db:
            member=db.query(GroupMember).filter_by(group_id=gid,user_id=ids[1]).one()
            member.presence={**member.presence,'updated_at':time.time()-5}; db.commit()
        assert not next(m for m in host.request.get(endpoint).json()['members'] if m['user_id']==ids[1])['presence']
        ws_url=endpoint.replace('http:', 'ws:')+'/presence/ws'
        for user_id, origin in [(None,base),(ids[2],base),(ids[0],'https://untrusted.example')]:
            try:
                with ws_connect(ws_url,origin=origin,additional_headers={'Cookie':'session='+cookie(user_id)} if user_id else {},proxy=None):
                    raise AssertionError('Unauthorized socket accepted')
            except InvalidStatus as error:
                assert error.response.status_code==403
        with ws_connect(ws_url,origin=base,additional_headers={'Cookie':'session='+cookie(ids[0])},proxy=None) as ws1, ws_connect(ws_url,origin=base,additional_headers={'Cookie':'session='+cookie(ids[1])},proxy=None) as ws2:
            assert json.loads(ws1.recv(timeout=2))['type']=='ready'
            assert json.loads(ws2.recv(timeout=2))['type']=='ready'
            unchanged=host.request.get(endpoint).json()
            timings=[]
            for i in range(12):
                started=time.monotonic()
                ws2.send(json.dumps({'pointer':{'key':'0,0','x':i/12,'y':.5}}))
                update=json.loads(ws1.recv(timeout=2))
                assert update['type']=='presence'
                assert update['members'][0]['presence']['pointer']['x']==i/12
                timings.append(time.monotonic()-started); time.sleep(.034)
            assert max(timings)<.2,timings
            assert host.request.get(endpoint).json()['revision']==unchanged['revision']
            # An HTTP-only player's position is also pushed to socket peers.
            assert player.request.post(endpoint+'/presence',data={'pointer':{'x':.9,'y':.8}}).ok
            assert json.loads(ws1.recv(timeout=2))['members'][0]['presence']['pointer']['x']==.9
            ws2.send('{"pointer":{"x":2,"y":0}}')
            try:
                ws2.recv(timeout=2); raise AssertionError('Invalid cursor accepted')
            except ConnectionClosed as error:
                assert error.code==1008
        print(f'WebSocket relay latency: max {max(timings)*1000:.0f} ms; auth/origin/validation/mixed HTTP checks passed.')
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
        # A committed action is pushed as a recipient-specific snapshot, without a GET.
        with ws_connect(ws_url,origin=base,additional_headers={'Cookie':'session='+cookie(ids[1])},proxy=None) as peer:
            assert json.loads(peer.recv(timeout=2))['type']=='ready'
            current=host.request.get(endpoint).json()['revision']
            started=time.monotonic(); committed=action(host,current,{k1:'0'}).json()
            pushed=json.loads(peer.recv(timeout=2))
            assert pushed['type']=='state' and pushed['snapshot']['revision']==committed['revision']
            assert pushed['snapshot']['user_id']==ids[1] and pushed['snapshot']['state']['answers'][k1]=='0'
            assert time.monotonic()-started<.5
            assert action(host,committed['revision'],{k1:''}).ok
            assert json.loads(peer.recv(timeout=2))['snapshot']['state']['answers'].get(k1,'')==''
        # Real UI, existing module handlers, poll updates, reload and independent personal state.
        a=host.new_page(); b=player.new_page(); errors=[]
        for page in [a,b]: page.on('pageerror',lambda error:errors.append(str(error))); page.goto(url)
        a.wait_for_function("!document.querySelector('.puzzle-layout > .panel').inert")
        b.wait_for_function("!document.querySelector('.puzzle-layout > .panel').inert")
        for page in [a,b]:
            page.wait_for_function('document.querySelector(".group-presence-layer").dataset.transport==="websocket"')
            page.route('**/api/groups/*',lambda route: route.abort() if route.request.method=='GET' else route.continue_())
        started=time.monotonic()
        a.locator(f'.takuzu-play [data-key="{k1}"]').press('1')
        b.wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="1"',arg=k1)
        assert time.monotonic()-started<.5,'Edits must not wait for polling or the old debounce'
        b.locator(f'.takuzu-play [data-key="{k2}"]').press('0')
        a.wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="0"',arg=k2)
        for page in [a,b]: page.unroute('**/api/groups/*')
        b.reload(); b.wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="1"',arg=k1)
        assert b.locator('#group-members').inner_text().count('online')==2
        assert b.locator('#group-host').is_hidden()
        # Remote pointer/focus overlays never become interactive board content.
        for page in [a,b]: page.evaluate('document.hasFocus = () => true')
        for page in [a,b]: page.wait_for_function('document.querySelector(".group-presence-layer").dataset.transport==="websocket"')
        remote=b.locator(f'.takuzu-play [data-key="{k2}"]')
        remote.focus(); remote.hover()
        a.wait_for_function('(id)=>!!document.querySelector(`.group-player-pointer[data-user="${id}"]:not([hidden])`)',arg=ids[1])
        a.wait_for_function('(id)=>!!document.querySelector(`.group-player-focus[data-user="${id}"]:not([hidden])`)',arg=ids[1])
        assert a.locator('.group-player-pointer span').inner_text()=='QA player'
        assert a.locator('.group-presence-layer').evaluate('(e)=>getComputedStyle(e).pointerEvents')=='none'
        a.evaluate("""() => {
          window.cursorFrames=[]; window.captureCursor=true;
          function capture(t) {
            const cursor=document.querySelector('.group-player-pointer:not([hidden])');
            if(cursor) window.cursorFrames.push({t,x:new DOMMatrixReadOnly(getComputedStyle(cursor).transform).m41});
            if(window.captureCursor) requestAnimationFrame(capture);
          }
          requestAnimationFrame(capture);
        }""")
        gesture=remote.bounding_box()
        for step in range(12):
            b.mouse.move(gesture['x']+gesture['width']*(.2+.6*(step%6)/5),gesture['y']+gesture['height']*.5)
            b.wait_for_timeout(35)
        a.wait_for_timeout(120); a.evaluate('window.captureCursor=false')
        frames=a.evaluate('window.cursorFrames')
        assert len({round(f['x'],1) for f in frames})>=15,frames
        assert abs(frames[-1]['x']-(gesture['x']+gesture['width']*.8))<3
        bounds=remote.bounding_box()
        ring=a.locator(f'.group-player-focus[data-user="{ids[1]}"]').bounding_box()
        assert abs(bounds['width']-ring['width'])<2
        a.screenshot(path='/tmp/group-presence-desktop.png',full_page=True)
        a.set_viewport_size({'width':390,'height':844})
        a.wait_for_timeout(1100)
        a.screenshot(path='/tmp/group-presence-mobile.png',full_page=True)
        mobile_ring=a.locator(f'.group-player-focus[data-user="{ids[1]}"]').bounding_box()
        mobile_cell=a.locator(f'.takuzu-play [data-key="{k2}"]').bounding_box()
        assert abs(mobile_ring['width']-mobile_cell['width'])<2
        assert abs(mobile_ring['x']-mobile_cell['x'])<2
        cursor=a.locator(f'.group-player-pointer[data-user="{ids[1]}"]').bounding_box()
        assert mobile_cell['x'] <= cursor['x'] <= mobile_cell['x']+mobile_cell['width']
        assert mobile_cell['y'] <= cursor['y'] <= mobile_cell['y']+mobile_cell['height']
        assert a.locator('.group-presence-layer').evaluate('(e)=>getComputedStyle(e).display')!='none'
        a.emulate_media(media='print')
        assert a.locator('.group-presence-layer').evaluate('(e)=>getComputedStyle(e).display')=='none'
        a.emulate_media(media='screen'); a.set_viewport_size({'width':1280,'height':1000})
        b.mouse.move(0,0)
        a.wait_for_function('(id)=>document.querySelector(`.group-player-pointer[data-user="${id}"]`).hidden',arg=ids[1])
        b.locator('#group-copy').focus()
        a.wait_for_function('(id)=>document.querySelector(`.group-player-focus[data-user="${id}"]`).hidden',arg=ids[1])
        # A delayed heartbeat response must not roll back an acknowledged edit.
        a.evaluate("""() => {
          window.originalGroupFetch = window.fetch; let hold = true;
          window.fetch = async (...args) => {
            const response = await window.originalGroupFetch(...args);
            if (hold && String(args[0]).startsWith('/api/groups/') && !args[1]?.method) {
              hold = false;
              return await new Promise(resolve => {window.releaseGroupHeartbeat = () => resolve(response)});
            }
            return response;
          };
        }""")
        a.wait_for_function('!!window.releaseGroupHeartbeat')
        a.locator(f'.takuzu-play [data-key="{k1}"]').press('0')
        b.wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="0"',arg=k1)
        a.evaluate('window.releaseGroupHeartbeat()'); a.wait_for_timeout(1300)
        assert a.locator(f'.takuzu-play [data-key="{k1}"]').inner_text()=='0'
        a.evaluate('window.fetch=window.originalGroupFetch')
        a.locator(f'.takuzu-play [data-key="{k1}"]').press('1')
        b.wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="1"',arg=k1)
        # Heartbeats must not rebuild controls, repaint the board or erase the invite.
        a.evaluate("""() => {
          window.memberButton = document.querySelector('#group-members button');
          window.boardMutations = 0;
          window.boardObserver = new MutationObserver(records => {window.boardMutations += records.length});
          window.boardObserver.observe(document.querySelector('.takuzu-play'), {subtree:true,childList:true,characterData:true});
          Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new Error('test clipboard unavailable')}}});
        }""")
        a.locator('#group-copy').click()
        link=a.locator('#group-invite-url')
        link.evaluate('(input)=>{input.focus();input.setSelectionRange(0,24)}')
        a.wait_for_timeout(2500)
        assert a.locator('#group-invite').evaluate('(e)=>e.open')
        assert link.input_value()==base+data['invite_url']
        assert a.locator('#group-message').inner_text()=='Clipboard unavailable. The invite link is shown above.'
        assert link.evaluate('(e)=>document.activeElement===e && e.selectionStart===0 && e.selectionEnd===24')
        assert a.evaluate("window.memberButton === document.querySelector('#group-members button')")
        assert a.evaluate('window.boardMutations')==0,'Unchanged heartbeats must not repaint the puzzle'
        a.evaluate("window.boardObserver.disconnect(); document.querySelector('#group-host').open=true")
        a.locator('#group-settings [name="participant_limit"]').fill('7')
        a.wait_for_timeout(1500)
        assert a.locator('#group-host').evaluate('(e)=>e.open')
        assert a.locator('#group-settings [name="participant_limit"]').input_value()=='7'
        a.locator('#group-settings [name="participant_limit"]').fill('0')
        # Clipboard success feedback is also independent from progress snapshots.
        a.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.copiedInvite=value}}})")
        a.locator('#group-copy').click(); a.wait_for_timeout(1500)
        assert a.evaluate('window.copiedInvite')==base+data['invite_url']
        assert a.locator('#group-message').inner_text()=='Invite link copied.'
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
        assert pending.request.post(endpoint+'/presence',data={}).status==403
        assert pending.request.post(endpoint+'/actions',data={'action_id':str(uuid.uuid4()),'revision':0}).status==403
        assert player.request.post(endpoint+'/members',data={'user_id':ids[3],'action':'approve'}).status==403
        assert host.request.post(endpoint+'/members',data={'user_id':ids[3],'action':'approve'}).ok
        assert pending.request.get(endpoint).ok
        with ws_connect(ws_url,origin=base,additional_headers={'Cookie':'session='+cookie(ids[3])},proxy=None) as revoked:
            assert json.loads(revoked.recv(timeout=2))['type']=='ready'
            assert host.request.post(endpoint+'/members',data={'user_id':ids[3],'action':'remove'}).ok
            try:
                while True: revoked.recv(timeout=4)
            except ConnectionClosed as error:
                assert error.code==1008
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
        assert player.request.post(endpoint+'/presence',data={}).status==409
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
            if kind=='nonogram':
                # Keep painting faster than the old trailing debounce and inspect before mouse-up.
                stroke=page1.locator('.nonogram-play button[data-key]:not([disabled])').all()[1:7]
                first=stroke[0].bounding_box(); stroke_key=stroke[0].get_attribute('data-key')
                page1.mouse.move(first['x']+first['width']/2,first['y']+first['height']/2); page1.mouse.down()
                for cell in stroke:
                    rect=cell.bounding_box()
                    page1.mouse.move(rect['x']+rect['width']/2,rect['y']+rect['height']/2)
                    page1.wait_for_timeout(50)
                assert page2.locator(f'.nonogram-play button[data-key="{stroke_key}"]').get_attribute('data-value')=='1', 'Painting must sync before the stroke ends'
                page1.mouse.up()
            if kind!='nonogram':
                for page in [page1,page2]: page.evaluate('document.hasFocus = () => true')
                # Remote changes to another cell must preserve the focused input's caret.
                page1.evaluate('(key)=>{const input=document.querySelector(`.puzzle-layout input[data-key="${key}"]`);input.focus();input.setSelectionRange(0,1)}',key)
                page2.wait_for_function('(id)=>!!document.querySelector(`.group-player-focus[data-user="${id}"]:not([hidden]) .group-player-caret:not([hidden])`)',arg=ids[0])
                page2.wait_for_function('(id)=>document.querySelector(`.group-player-focus[data-user="${id}"]`)?.classList.contains("has-selection")',arg=ids[0])
                if kind=='crossword':
                    page2.wait_for_function('!!document.querySelector(".group-player-entry:not([hidden])")')
                other_cell=page2.locator('.puzzle-layout input[data-key]:not([disabled])').nth(1)
                other_cell.press('B' if kind=='crossword' else '2')
                other_key=other_cell.get_attribute('data-key'); other_value='B' if kind=='crossword' else '2'
                page1.wait_for_function('([key,value])=>document.querySelector(`.puzzle-layout input[data-key="${key}"]`).value===value',arg=[other_key,other_value])
                assert editable_cell.evaluate('(e)=>document.activeElement===e && e.selectionStart===0 && e.selectionEnd===1')
            if kind=='crossword':
                more=page1.locator('.more-clue').first
                if more.count():
                    more.click()
                    page2.wait_for_function("[...document.querySelectorAll('.clue-steps')].some(e=>!e.hidden)")
                page1.locator('.reveal-entry:not([disabled])').first.click(); page1.wait_for_timeout(500)
                assert host.request.get(endpoint2).json()['state']['hints_used']>=1
            page1.locator('#group-check').click(); page1.wait_for_timeout(400)
            revision=host.request.get(endpoint2).json()['revision']
            for page in [page1,page2]:
                page.wait_for_function('(revision)=>Number(document.querySelector("#group-solve").dataset.revision)>=revision',arg=revision)
            if kind!='nonogram':
                with SessionLocal() as db:
                    item=db.get(Puzzle,'qa-'+kind)
                    solution=item.solution_payload.get('grid',item.solution_payload.get('answers',{}))
                    x,y=map(int,key.split(',')); expected=str(solution[key] if isinstance(solution,dict) else solution[y][x])
                changed=page2.locator(f'.puzzle-layout input[data-key="{key}"]')
                if changed.input_value()!=expected:
                    changed.fill(expected); changed.dispatch_event('input')
                page1.wait_for_function('(key)=>document.querySelector(`.puzzle-layout input[data-key="${key}"]`).parentElement.classList.contains("correct")',arg=key)
                try:
                    page2.wait_for_function('(key)=>document.querySelector(`.puzzle-layout input[data-key="${key}"]`).parentElement.classList.contains("correct")',arg=key,timeout=5000)
                except Exception as error:
                    raise AssertionError((kind,key,expected,changed.input_value(),host.request.get(endpoint2).json()['state'],page2.locator('#group-status').inner_text())) from error
                wrong='Z' if kind=='crossword' else '1' if expected!='1' else '2'
                changed.fill(wrong); changed.dispatch_event('input')
                try:
                    page1.wait_for_function('(key)=>document.querySelector(`.puzzle-layout input[data-key="${key}"]`).parentElement.classList.contains("incorrect")',arg=key,timeout=5000)
                except Exception as error:
                    raise AssertionError((kind,expected,wrong,host.request.get(endpoint2).json()['state'],page2.locator('#group-status').inner_text())) from error
                assert not editable_cell.evaluate('(e)=>e.parentElement.classList.contains("correct")')
            page1.get_by_text('Hints',exact=True).click(); page1.locator('#reveal-solution').click()
            page2.wait_for_function("document.querySelector('#group-status').textContent==='Group puzzle complete.'")
            assert host.request.get(endpoint2).json()['completed']
            page1.close(); page2.close()
        # Proxy-blocked WebSockets fall back and automatically upgrade after recovery.
        fallback_info=host.request.post(base+'/api/puzzles/group-qa/groups',data={}).json()
        normal=player.new_page(); fallback=host.new_page()
        fallback.add_init_script("""window.NativeWebSocket=window.WebSocket;
          window.WebSocket=class {static OPEN=1; readyState=3; bufferedAmount=0;
            addEventListener(type,callback){if(type==='close')setTimeout(()=>callback({code:1006}),0)} close(){} };""")
        for page in [normal,fallback]:
            page.on('pageerror',lambda error:errors.append(str(error))); page.goto(base+fallback_info['url'])
            page.wait_for_function("!document.querySelector('.puzzle-layout > .panel').inert")
            page.evaluate('document.hasFocus=()=>true')
        normal.wait_for_function('document.querySelector(".group-presence-layer").dataset.transport==="websocket"')
        fallback.locator(f'.takuzu-play [data-key="{k1}"]').hover()
        normal.wait_for_function('(id)=>!!document.querySelector(`.group-player-pointer[data-user="${id}"]:not([hidden])`)',arg=ids[0])
        assert fallback.locator('.group-presence-layer').get_attribute('data-transport')=='polling'
        fallback.evaluate('() => {window.WebSocket=window.NativeWebSocket;}')
        fallback.wait_for_function('document.querySelector(".group-presence-layer").dataset.transport==="websocket"',timeout=6000)
        normal.close(); fallback.close()
        assert not errors,errors
        from verify_group_guests import verify_guests
        verify_guests(browser, base, host, player, ids[0])
        from verify_group_moves import verify_moves
        verify_moves(browser, base, host, player)
        browser.close()
    print('Group API/auth/conflict/idempotency/approval/limits/removal/persistence/isolation and two-browser desktop/mobile tests passed.')
finally:
    server.terminate(); server.wait(timeout=10)
