"""Opt-in PostgreSQL concurrency and live browser smoke test; removes its own group."""
import os
from pathlib import Path
import tempfile
if os.environ.get('PUZZARY_LIVE_GROUP_QA') != '1':
    raise SystemExit('Set PUZZARY_LIVE_GROUP_QA=1 to run against the live service')
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import uuid
import time
from websockets.sync.client import connect as ws_connect

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from app.database import SessionLocal
from app.main import SESSION_SECRET
from app.models import GroupSolve, Puzzle, PuzzleProgress, User

base='http://127.0.0.1:8790'
with SessionLocal() as db:
    host=db.scalar(select(User).where(User.is_active==True, User.is_admin==True).order_by(User.id))
    player=db.scalar(select(User).where(User.is_active==True, User.is_admin==False).order_by(User.id))
    if not host or not player: raise RuntimeError('Two existing active accounts are required')
    ids=[host.id,player.id]
    cookies=[TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({'user_id':u.id,'session_version':u.session_version}).encode())).decode() for u in [host,player]]
    puzzle=db.scalar(select(Puzzle).where(Puzzle.puzzle_type=='takuzu').order_by(Puzzle.created_at.desc()))
    if not puzzle: raise RuntimeError('An existing Takuzu puzzle is required')
    pid=puzzle.id; grid=puzzle.solution_payload['grid']; givens=puzzle.solution_payload['givens']
    keys=[f'{x},{y}' for y,row in enumerate(grid) for x,v in enumerate(row) if f'{x},{y}' not in givens]
    original={u:db.scalar(select(PuzzleProgress).where(PuzzleProgress.puzzle_id==pid,PuzzleProgress.user_id==u)).progress_payload
              if db.scalar(select(PuzzleProgress).where(PuzzleProgress.puzzle_id==pid,PuzzleProgress.user_id==u)) else None for u in ids}

def request(index,path,body=None):
    headers={'Cookie':'session='+cookies[index]}
    if body is not None: headers['Content-Type']='application/json'
    req=Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
    try:
        with urlopen(req,timeout=15) as response: return response.status,json.load(response)
    except HTTPError as error: return error.code,json.load(error)

gid=None
try:
    status,data=request(0,f'/api/puzzles/{pid}/groups',{})
    assert status==200,data
    gid=data['id']; endpoint='/api/groups/'+gid
    assert request(1,'/api/groups/join',{'code':data['code']})[0]==200
    origins=[base]
    if os.environ.get('PUZZARY_QA_PUBLIC_ORIGIN'): origins.append(os.environ['PUZZARY_QA_PUBLIC_ORIGIN'].rstrip('/'))
    for origin in origins:
        ws_url=origin.replace('https:', 'wss:').replace('http:', 'ws:')+endpoint+'/presence/ws'
        with ws_connect(ws_url,origin=origin,additional_headers={'Cookie':'session='+cookies[0]},proxy=None) as ws1, ws_connect(ws_url,origin=origin,additional_headers={'Cookie':'session='+cookies[1]},proxy=None) as ws2:
            assert json.loads(ws1.recv(timeout=5))['type']=='ready'
            assert json.loads(ws2.recv(timeout=5))['type']=='ready'
            delays=[]
            for i in range(8):
                started=time.monotonic(); ws2.send(json.dumps({'pointer':{'key':keys[0],'x':i/8,'y':.5}}))
                message=json.loads(ws1.recv(timeout=5)); assert message['members'][0]['presence']['pointer']['x']==i/8
                delays.append(time.monotonic()-started); time.sleep(.04)
            print(f'Live cursor relay ({origin}): max {max(delays)*1000:.0f} ms')
            assert max(delays)<1,delays
            revision=request(0,endpoint)[1]['revision']
            started=time.monotonic()
            status,committed=request(0,endpoint+'/actions',{'action_id':str(uuid.uuid4()),'revision':revision,'kind':'patch','changes':{keys[0]:'0'}})
            assert status==200,committed
            while True:
                pushed=json.loads(ws2.recv(timeout=5))
                if pushed['type']=='state': break
            assert pushed['snapshot']['user_id']==ids[1]
            assert pushed['snapshot']['revision']>=committed['revision']
            assert pushed['snapshot']['state']['answers'][keys[0]]=='0'
            print(f'Live committed puzzle push ({origin}): {(time.monotonic()-started)*1000:.0f} ms')
    def pair(changes):
        barrier=Barrier(2); revision=request(0,endpoint)[1]['revision']
        def edit(index):
            barrier.wait()
            return request(index,endpoint+'/actions',{'action_id':str(uuid.uuid4()),'revision':revision,'kind':'patch','changes':changes[index]})[0]
        with ThreadPoolExecutor(2) as pool: return list(pool.map(edit,[0,1]))
    assert pair([{keys[0]:'0'},{keys[1]:'1'}])==[200,200]
    competing=pair([{keys[0]:'1'},{keys[0]:'1'}])
    assert sorted(competing)==[200,409],competing
    with sync_playwright() as p:
        browser=p.chromium.launch(); pages=[]; errors=[]
        for value in cookies:
            context=browser.new_context(viewport={'width':1280,'height':900},has_touch=True)
            context.add_cookies([{'name':'session','value':value,'url':base}])
            page=context.new_page(); page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+data['url']); page.wait_for_function("!document.querySelector('.puzzle-layout > .panel').inert")
            pages.append(page)
        for page in pages:
            page.wait_for_function('document.querySelector(".group-presence-layer").dataset.transport==="websocket"')
            page.route('**/api/groups/*',lambda route: route.abort() if route.request.method=='GET' else route.continue_())
        started=time.monotonic()
        pages[0].locator(f'.takuzu-play [data-key="{keys[1]}"]').press('0')
        pages[1].wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="0"',arg=keys[1])
        print(f'Live browser input-to-peer paint: {(time.monotonic()-started)*1000:.0f} ms (snapshot polling blocked)')
        assert time.monotonic()-started<.5
        for page in pages: page.unroute('**/api/groups/*')
        pages[1].reload(); pages[1].wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="0"',arg=keys[1])
        pages[0].evaluate("""() => {
          window.liveMemberButton = document.querySelector('#group-members button');
          window.liveBoardChanges = 0;
          window.liveBoardObserver = new MutationObserver(records => {window.liveBoardChanges += records.length});
          window.liveBoardObserver.observe(document.querySelector('.takuzu-play'),{subtree:true,childList:true,characterData:true});
          Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new Error('mock clipboard unavailable')}}});
        }""")
        pages[0].locator('#group-copy').click()
        field=pages[0].locator('#group-invite-url')
        field.evaluate('(e)=>{e.focus();e.setSelectionRange(0,20)}')
        pages[0].wait_for_timeout(2500)
        assert field.input_value()==base+data['invite_url']
        assert pages[0].locator('#group-invite').evaluate('(e)=>e.open')
        assert 'Clipboard unavailable' in pages[0].locator('#group-message').inner_text()
        assert field.evaluate('(e)=>document.activeElement===e && e.selectionStart===0 && e.selectionEnd===20')
        assert pages[0].evaluate('window.liveBoardChanges')==0
        assert pages[0].evaluate("window.liveMemberButton===document.querySelector('#group-members button')")
        pages[0].evaluate('window.liveBoardObserver.disconnect()')
        for page in pages: page.evaluate('document.hasFocus = () => true')
        for page in pages: page.wait_for_function('document.querySelector(".group-presence-layer").dataset.transport==="websocket"')
        remote=pages[1].locator(f'.takuzu-play [data-key="{keys[0]}"]')
        remote.focus(); remote.hover()
        pages[0].wait_for_function('(id)=>!!document.querySelector(`.group-player-pointer[data-user="${id}"]:not([hidden])`)',arg=ids[1])
        pages[0].wait_for_function('(id)=>!!document.querySelector(`.group-player-focus[data-user="${id}"]:not([hidden])`)',arg=ids[1])
        assert pages[0].locator('.group-presence-layer').evaluate('(e)=>getComputedStyle(e).pointerEvents')=='none'
        pages[0].screenshot(path=str(Path(tempfile.gettempdir()) / 'group-presence-live-desktop.png'),full_page=True)
        pages[0].set_viewport_size({'width':390,'height':844}); pages[0].wait_for_timeout(500)
        pages[0].locator(f'.takuzu-play [data-key="{keys[0]}"]').scroll_into_view_if_needed()
        pages[0].wait_for_timeout(300)
        pages[0].screenshot(path=str(Path(tempfile.gettempdir()) / 'group-presence-live-mobile.png'))
        remote.tap()
        pages[0].wait_for_function('(id)=>document.querySelector(`.group-player-pointer[data-user="${id}"]`).hidden',arg=ids[1])
        assert not pages[0].locator(f'.group-player-focus[data-user="{ids[1]}"]').is_hidden()
        assert pages[1].get_by_role('heading',name='Admin Solution').count()==0
        # Guest identity is cookie-bound and confined to this temporary group.
        assert request(0, endpoint+'/settings', {'allow_guests': True})[0] == 200
        guest = browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
        gp = guest.new_page(); gp.on('pageerror',lambda error:errors.append(str(error)))
        gp.goto(base+data['url'])
        gp.locator('#guest-join [name=name]').fill('QA guest '+gid[:8])
        gp.locator('#guest-join button').click()
        gp.wait_for_function("document.querySelector('#group-solve')?.dataset.revision!==undefined")
        gp.wait_for_function('document.querySelector(".group-presence-layer").dataset.transport==="websocket"')
        identity = guest.request.get(base+endpoint).json()
        guest_id = identity['user_id']
        assert identity['is_guest'] and guest_id < 0 and not identity['can_allow_guests']
        assert guest.request.get(base+f'/api/puzzles/{pid}/progress').status == 401
        assert gp.get_by_role('heading',name='Admin Solution').count() == 0
        gp.locator(f'.takuzu-play [data-key="{keys[2]}"]').press('1')
        pages[0].wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="1"',arg=keys[2])
        gp.reload()
        gp.wait_for_function('document.querySelector(".group-presence-layer").dataset.transport==="websocket"')
        assert guest.request.get(base+endpoint).json()['user_id'] == guest_id
        gp.screenshot(path=str(Path(tempfile.gettempdir()) / 'group-guest-live-mobile.png'),full_page=True)
        guest_cookie = next(c['value'] for c in guest.cookies() if c['name']=='session')
        for origin in origins:
            guest_ws = origin.replace('https:','wss:').replace('http:','ws:')+endpoint+'/presence/ws'
            with ws_connect(guest_ws,origin=origin,additional_headers={'Cookie':'session='+guest_cookie},proxy=None) as socket:
                initial = json.loads(socket.recv(timeout=5))
                assert initial['type']=='ready' and initial['snapshot']['user_id']==guest_id
        assert request(0,endpoint+'/settings',{'allow_guests':False})[0] == 200
        assert guest.request.get(base+endpoint).status==401
        gp.wait_for_function("document.querySelector('.puzzle-layout > .panel').inert",timeout=15000)
        print('Live mobile guest join/reload/edit, LAN/public WebSocket access and disabling guests passed.')
        assert not errors,errors
        browser.close()
    with SessionLocal() as db:
        for user_id in ids:
            progress=db.scalar(select(PuzzleProgress).where(PuzzleProgress.puzzle_id==pid,PuzzleProgress.user_id==user_id))
            assert (progress.progress_payload if progress else None)==original[user_id]
    print('Live PostgreSQL simultaneous edits, same-cell conflict, two-account sync/reload and personal isolation passed.')
finally:
    if gid:
        with SessionLocal() as db:
            group=db.get(GroupSolve,gid)
            if group: db.delete(group); db.commit()
