"""Opt-in PostgreSQL concurrency and live browser smoke test; removes its own group."""
import os
if os.environ.get('PUZZARY_LIVE_GROUP_QA') != '1':
    raise SystemExit('Set PUZZARY_LIVE_GROUP_QA=1 to run against the live service')
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import uuid

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
            context=browser.new_context(viewport={'width':1280,'height':900})
            context.add_cookies([{'name':'session','value':value,'url':base}])
            page=context.new_page(); page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+data['url']); page.wait_for_function("!document.querySelector('.puzzle-layout > .panel').inert")
            pages.append(page)
        pages[0].locator(f'.takuzu-play [data-key="{keys[1]}"]').press('0')
        pages[1].wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="0"',arg=keys[1])
        pages[1].reload(); pages[1].wait_for_function('(key)=>document.querySelector(`.takuzu-play [data-key="${key}"]`).dataset.value==="0"',arg=keys[1])
        assert pages[1].get_by_role('heading',name='Admin Solution').count()==0
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
