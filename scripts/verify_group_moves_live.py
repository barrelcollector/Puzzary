"""Opt-in live host/guest switching QA; all created groups/puzzles are temporary."""
import os
if os.environ.get('PUZZARY_LIVE_GROUP_QA') != '1':
    raise SystemExit('Set PUZZARY_LIVE_GROUP_QA=1 to run against the live service')
import base64
import json
import uuid
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from app.database import SessionLocal
from app.main import SESSION_SECRET
from app.models import GroupSolve, Puzzle, User
from app.puzzles import get_puzzle_type
from verify_group_moves import verify_moves

base = 'http://127.0.0.1:8790'
suffix = uuid.uuid4().hex[:10]
puzzle_ids = ['qa-move-'+suffix, 'qa-next-'+suffix]
with SessionLocal() as db:
    accounts = [db.scalar(select(User).where(User.is_active==True,User.is_admin==admin).order_by(User.id)) for admin in [True,False]]
    if not all(accounts):
        raise RuntimeError('Active admin and player accounts are required')
    cookies = [TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({'user_id':u.id,'session_version':u.session_version}).encode())).decode() for u in accounts]
    grid = [[0,0,1,1],[0,1,0,1],[1,0,1,0],[1,1,0,0]]
    first = get_puzzle_type('takuzu').manual_builder({'solution_grid':grid,'given_count':0,'seed':'move-qa'})
    for payload in [first.play_payload,first.print_payload,first.solution_payload]:
        payload['givens'] = {}
    second = get_puzzle_type('sudoku').generator('', 'easy', None, 'move-qa', {'grid_size':4,'given_count':6})
    for pid, kind, item in zip(puzzle_ids,['takuzu','sudoku'],[first,second]):
        db.add(Puzzle(id=pid,puzzle_type=kind,title='QA Group Move '+kind,theme='',difficulty='easy',
                      prompt=item.prompt,play_payload=item.play_payload,print_payload=item.print_payload,
                      solution_payload=item.solution_payload,source='test',puzzle_metadata={}))
    db.commit()
try:
    with sync_playwright() as p:
        browser=p.chromium.launch()
        contexts=[browser.new_context(viewport={'width':1280,'height':1000}) for _ in cookies]
        for context,value in zip(contexts,cookies):
            context.add_cookies([{'name':'session','value':value,'url':base}])
        verify_moves(browser,base,*contexts,first=puzzle_ids[0],second=puzzle_ids[1])
        browser.close()
    print('Live PostgreSQL switching, progress restoration and three-browser host/player/guest navigation passed.')
finally:
    with SessionLocal() as db:
        for group in db.scalars(select(GroupSolve).where(GroupSolve.puzzle_id.in_(puzzle_ids))):
            db.delete(group)
        db.flush()
        for pid in puzzle_ids:
            item=db.get(Puzzle,pid)
            if item: db.delete(item)
        db.commit()
