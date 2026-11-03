"""Isolated multi-account library, statistics, archive and reset regression checks."""
import os
os.environ['DATABASE_URL'] = os.getenv('ACTIVITY_QA_DATABASE', 'sqlite:////tmp/puzzary-activity-qa.sqlite')
os.environ['SESSION_SECRET'] = 'isolated-activity-test-secret'
os.environ['UPLOAD_DIR'] = '/tmp/activity-uploads'

import base64
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import inspect, select
from app.database import Base, engine, SessionLocal, init_db
from app.models import User, Puzzle, PuzzleActivityEvent, PuzzleHint, PuzzleProgress, PuzzleAttempt, PermissionRole
from app.permissions import initialize_permissions
from app.main import hash_secret, hash_password, SESSION_SECRET
from app.puzzles import get_puzzle_type
from app.user_activity import account_activity, progress_states

Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
PuzzleActivityEvent.__table__.drop(engine)
init_db()
assert 'puzzle_activity_events' in inspect(engine).get_table_names()
with SessionLocal() as db:
    admin = User(display_name='admin', is_admin=True, is_active=True, password_hash=hash_password('isolated-activity-password'))
    db.add(admin)
    db.commit()
    initialize_permissions(db)
    role = db.scalar(select(PermissionRole).where(PermissionRole.name == 'Player'))
    alice = User(display_name='Alice', is_active=True, roles=[role], permissions_initialized=True)
    bob = User(display_name='Bob', is_active=True, roles=[role], permissions_initialized=True,
               api_token_hash=hash_secret('isolated-activity-token'))
    db.add_all([alice,bob]); db.flush()
    ids = {'admin':admin.id, 'alice':alice.id, 'bob':bob.id}
    generated = get_puzzle_type('takuzu').manual_builder({'solution_grid':[[0,0,1,1],[0,1,0,1],[1,0,1,0],[1,1,0,0]],'given_count':0,'seed':'activity-fixture'})
    complete = {f'{x},{y}':str(value) for y,row in enumerate(generated.solution_payload['grid']) for x,value in enumerate(row)}
    wrong_key = next(key for key in complete if key not in generated.solution_payload['givens'])
    wrong_guess = {wrong_key:str(1-int(complete[wrong_key]))}
    for pid in ['alpha','beta','empty','archived','private']:
        db.add(Puzzle(id=pid,puzzle_type='takuzu',title=pid,theme='',difficulty='easy',created_by_id=admin.id,
                      print_payload=generated.print_payload,play_payload=generated.play_payload,solution_payload=generated.solution_payload,
                      puzzle_metadata={'draft':pid=='private'},auto_archive_at=None,
                      archived_at=datetime.now(timezone.utc) if pid in ['archived','private'] else None,source='test'))
    db.add(Puzzle(id='gimme',puzzle_type='word_search',title='Gimme fixture',theme='',difficulty='easy',created_by_id=admin.id,
                  print_payload={'grid':['CAT','DOG','XYZ'],'words':['CAT','DOG']},play_payload={},
                  solution_payload={'words':['CAT','DOG'],'gimmes':{'words':['CAT']}},puzzle_metadata={},auto_archive_at=None,source='test'))
    db.add(PuzzleHint(puzzle_id='empty', order=1, text='A durable general hint.'))
    db.flush()
    for pid,uid,payload in [('alpha',alice.id,{'answers':complete}),('alpha',bob.id,{'answers':wrong_guess}),
                            ('empty',alice.id,{'answers':{}}),('gimme',alice.id,{'found_words':['CAT']}),
                            ('gimme',bob.id,{'found_words':['CAT','DOG']})]:
        db.add(PuzzleProgress(puzzle_id=pid,user_id=uid,progress_payload=payload))
    for pid,correct in [('beta',False),('beta',False),('beta',True),('archived',False)]:
        db.add(PuzzleAttempt(puzzle_id=pid,user_id=alice.id,answer='isolated check',is_correct=correct))
    db.commit()
    puzzles = db.scalars(select(Puzzle)).all()
    a = progress_states(db,alice,puzzles); b = progress_states(db,bob,puzzles)
    assert a['alpha']['status']=='completed' and b['alpha']['status']=='in_progress'
    assert a['beta']['status']=='completed' and b['beta']['status']=='not_started'
    assert a['gimme']['status']=='not_started' and b['gimme']['status']=='completed'
    assert a['empty']['status']=='not_started'
    assert account_activity(db,admin)['totals']['played']==0
    assert account_activity(db,alice)['totals']=={'played':3,'completed':2,'in_progress':1,'attempts':4,'correct_attempts':1}
    assert account_activity(db,bob)['totals']=={'played':2,'completed':1,'in_progress':1,'attempts':0,'correct_attempts':0}
    # The same plugin contract excludes starter rows/cells, but preserves wrong guesses and binary zero.
    for slug,solution,payload in [
        ('crossword',{'grid':[['C','A','T']],'gimmes':{'cells':{'0,0':'C'}}},{'answers':{'0,0':'C'}}),
        ('word_pyramid',{'rows':[{'answer':'CAT','gimme':True},{'answer':'CATS'}]},{'answers':{'0':'CAT'}}),
        ('sudoku',{'grid':[[1,2],[2,1]],'givens':{'0,0':'1'}},{'answers':{'0,0':'1'}}),
    ]:
        from app.user_activity import _changed_from_starters
        plugin = get_puzzle_type(slug)
        baseline = plugin.progress_normalizer(None,solution)
        from app.schemas import PuzzleProgressUpdate
        assert not _changed_from_starters(plugin.progress_normalizer(PuzzleProgressUpdate.model_validate(payload),solution),baseline),slug
    assert _changed_from_starters({'answers':{'0,0':'0'}},{'answers':{}})
init_db()

base='http://127.0.0.1:8793'
def cookie(uid):
    return TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({'user_id':uid,'session_version':1}).encode())).decode()
server=subprocess.Popen(['uvicorn','app.main:app','--host','127.0.0.1','--port','8793'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
try:
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        contexts={}
        for name,uid in ids.items():
            context=browser.new_context()
            context.add_cookies([{'name':'session','value':cookie(uid),'url':base}])
            contexts[name]=context
        admin,alice,bob=[contexts[name] for name in ['admin','alice','bob']]
        for _ in range(100):
            try:
                if admin.request.get(base+'/health').ok: break
            except Exception: time.sleep(.1)
        else: raise RuntimeError('QA server did not start')
        def library(context,query=''):
            response=context.request.get(base+'/api/puzzles'+query)
            assert response.ok,response.text()
            data=response.json()
            return {row['id']:row for row in data['items']},data
        a,data=library(alice); b,_=library(bob)
        assert data['count']==4 and set(a)=={'alpha','beta','empty','gimme'}
        assert a['alpha']['progress']['status']=='completed' and b['alpha']['progress']['status']=='in_progress'
        assert set(library(alice,'?status=completed')[0])=={'alpha','beta'}
        assert set(library(bob,'?status=completed')[0])=={'gimme'}
        assert not library(alice,'?availability=archived')[0]
        archived,_=library(admin,'?availability=archived')
        assert 'archived' in archived and archived['archived']['progress']['status']=='not_started'
        assert not library(alice,'?created_by=me')[0]
        assert alice.request.get(base+'/api/account/stats?user_id='+str(ids['bob'])).json()['user_id']==ids['alice']
        assert browser.new_context().request.get(base+'/api/account/stats').status==401
        token=browser.new_context(extra_http_headers={'Authorization':'Bearer isolated-activity-token'})
        assert token.request.get(base+'/api/account/stats').json()['user_id']==ids['bob']
        stats=alice.request.get(base+'/api/account/stats').json()
        assert stats['totals']['completed']==2 and stats['totals']['attempts']==4
        assert stats['by_type'][0]['puzzle_type']=='takuzu' and stats['by_type'][0]['attempts']==4
        assert next(row for row in stats['recent'] if row['id']=='archived')['url'] is None
        assert alice.request.post(base+'/api/puzzles/empty/progress',data={'answers':wrong_guess}).ok
        checked=alice.request.post(base+'/api/puzzles/empty/actions',data={'kind':'check','changes':{}})
        assert checked.ok,checked.text()
        hinted=alice.request.post(base+'/api/puzzles/empty/actions',data={'kind':'hint','changes':{}})
        assert hinted.ok,hinted.text()
        revealed=alice.request.post(base+'/api/puzzles/empty/actions',data={'kind':'reveal','changes':{}})
        assert revealed.ok,revealed.text()
        hint_page=alice.new_page(); assert hint_page.goto(base+'/puzzles/empty').ok
        with hint_page.expect_response(lambda response: response.url.endswith('/api/puzzles/empty/activity')):
            hint_page.locator('[data-puzzle-hints] summary').click()
        hint_page.close()
        assert alice.request.post(base+'/api/puzzles/empty/reset').ok
        history=alice.request.get(base+'/api/account/stats').json()['history']
        empty_types=[row['event_type'] for row in history if row['puzzle_id']=='empty']
        assert {'started','attempt','hint','reveal','completion','reset'} <= set(empty_types),empty_types
        output=Path('/tmp/activity-qa'); output.mkdir(exist_ok=True)
        errors=[]
        for width in [1280,390]:
            page=alice.new_page(); page.set_viewport_size({'width':width,'height':900})
            page.on('pageerror',lambda error: errors.append(str(error)))
            assert page.goto(base+'/account').ok
            assert page.locator('[data-stat="completed"]').inner_text()=='2'
            assert page.locator('[data-history-event="reset"]').count()>=1
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.screenshot(path=str(output/f'account-{width}.png'),full_page=True)
            assert page.goto(base+'/account/password').ok
            assert page.locator('input[name="password"]').count()==1
            assert page.goto(base+'/library').ok
            assert page.locator('[data-library-puzzle="alpha"]').get_attribute('data-progress-status')=='completed'
            assert page.locator('[data-library-puzzle="beta"]').get_attribute('data-progress-status')=='completed'
            assert page.locator('[data-library-puzzle="gimme"]').get_attribute('data-progress-status')=='not_started'
            page.screenshot(path=str(output/f'library-{width}.png'),full_page=True)
            page.close()
            page=admin.new_page(); page.set_viewport_size({'width':width,'height':900})
            assert page.goto(base+f'/admin/users/{ids["alice"]}?tab=activity').ok
            assert page.locator('[data-stat="completed"]').inner_text()=='2'
            assert page.locator('[data-history-event="completion"]').count()>=1
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.screenshot(path=str(output/f'admin-activity-{width}.png'),full_page=True)
            page.close()
        assert not errors,errors
        with SessionLocal() as db:
            db.get(User,ids['bob']).permission_overrides={'users.view':True}; db.commit()
        response=bob.request.get(base+f'/admin/users/{ids["alice"]}?tab=activity')
        assert response.ok and 'data-account-stats' not in response.text()
        assert alice.request.post(base+'/api/puzzles/alpha/reset').ok
        assert library(alice)[0]['alpha']['progress']['status']=='not_started'
        assert library(bob)[0]['alpha']['progress']['status']=='in_progress'
        assert alice.request.get(base+'/api/account/stats').json()['totals']['completed']==1
        assert alice.request.post(base+'/api/puzzles/beta/reset').ok
        assert library(alice)[0]['beta']['progress']['status']=='not_started'
        assert alice.request.get(base+'/api/account/stats').json()['totals']['attempts']==1
        global_activity=admin.new_page(); assert global_activity.goto(base+'/admin/activity').ok
        assert global_activity.locator('[data-history-event="reset"]').count()>=1
        global_activity.close()
        with SessionLocal() as db:
            assert db.scalar(select(PuzzleActivityEvent).where(
                PuzzleActivityEvent.user_id==ids['alice'], PuzzleActivityEvent.event_type=='reset'
            )) is not None
        server.terminate(); server.wait(timeout=10)
        server=subprocess.Popen(['uvicorn','app.main:app','--host','127.0.0.1','--port','8793'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        for _ in range(100):
            try:
                if alice.request.get(base+'/health').ok: break
            except Exception: time.sleep(.1)
        else: raise RuntimeError('QA server did not restart')
        restarted_history=alice.request.get(base+'/api/account/stats').json()['history']
        assert any(row['event_type']=='reset' and row['puzzle_id']=='empty' for row in restarted_history)
        browser.close()
    print('Account isolation, durable attempts/hints/reveals/completions/resets across restart, archive visibility, permissions, replay resets and desktop/mobile views passed.')
finally:
    server.terminate()
    try: server.wait(timeout=10)
    except subprocess.TimeoutExpired: server.kill(); server.wait()
    if server.returncode not in [0,-15]: print(server.stderr.read().decode()[-5000:])
