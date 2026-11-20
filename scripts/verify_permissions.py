"""Isolated RBAC migration, browser/API authorization, revocation and UI QA."""
import os
from qa_runtime import qa_directory

qa_root = qa_directory('permissions')
qa_output = qa_root / 'screenshots'
qa_output.mkdir(exist_ok=True)
os.environ['DATABASE_URL'] = os.getenv('PERMISSIONS_QA_DATABASE', f"sqlite:///{(qa_root / 'permissions.sqlite').as_posix()}")
os.environ['SESSION_SECRET'] = 'isolated-permissions-test-secret'
os.environ['UPLOAD_DIR'] = str(qa_root / 'uploads')
import base64
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from urllib.parse import parse_qs, urlsplit
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed
from sqlalchemy import select, text
from fastapi import HTTPException
from app.database import Base, engine, SessionLocal, ensure_schema, init_db
from app.models import User, Puzzle, PuzzleProgress, PermissionRole, PermissionAudit
from app.permissions import initialize_permissions, effective_permissions, require, lock_permissions, protect_last_admin, register_permission, register_route_permission, ADMIN_CORE
from app.main import hash_password, hash_secret, SESSION_SECRET, ensure_admin_user
from app.puzzles import get_puzzle_type

Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
with SessionLocal() as db:
    admin = User(display_name='admin', is_admin=True, is_active=True, password_hash=hash_password('isolated-password'))
    old = User(display_name='Legacy', is_active=True)
    db.add_all([admin,old]); db.flush(); admin_id,old_id = admin.id,old.id
    generated = get_puzzle_type('takuzu').manual_builder({'solution_grid':[[0,0,1,1],[0,1,0,1],[1,0,1,0],[1,1,0,0]],'given_count':0})
    for name, owner, draft in [('mine',old_id,False),('private',admin_id,True)]:
        db.add(Puzzle(id=name,puzzle_type='takuzu',title=name,theme='',difficulty='easy',created_by_id=owner,
            print_payload=generated.print_payload,play_payload=generated.play_payload,solution_payload=generated.solution_payload,
            puzzle_metadata={'draft':draft},source='test'))
    db.add(PuzzleProgress(puzzle_id='mine',user_id=old_id,progress_payload={'answers':{'0,0':'1'}}))
    db.commit()
with engine.begin() as connection:
    connection.execute(text('ALTER TABLE users DROP COLUMN permissions_initialized'))
    connection.execute(text('ALTER TABLE users DROP COLUMN permission_overrides'))
ensure_schema(); init_db()
with SessionLocal() as db:
    initialize_permissions(db)
    assert db.get(User,admin_id).can('roles.manage')
    old = db.get(User,old_id)
    assert old.can('puzzles.generate') and not old.can('users.create') and not old.can('content.manage')
    assert db.scalar(select(PuzzleProgress)).progress_payload['answers']=={'0,0':'1'}
    initialize_permissions(db)
    assert len(old.roles)==1
    roles={r.name:r for r in db.scalars(select(PermissionRole))}
    assert {'Player','Publisher','Manager','Admin'} <= roles.keys()
    ids={}
    for name,role in [('Player','Player'),('Publisher','Publisher'),('Manager','Manager'),('Second admin','Admin')]:
        u=User(display_name=name,is_active=True,is_admin=role=='Admin',permissions_initialized=True,
               permission_overrides={},roles=[roles[role]],password_hash=hash_password('isolated-password'))
        db.add(u); db.flush(); ids[name]=u.id
    publisher=db.get(User,ids['Publisher'])
    publisher.api_token_hash=hash_secret('isolated-permission-token')
    db.get(Puzzle,'mine').created_by_id=publisher.id
    player=db.get(User,ids['Player']); player.roles.append(roles['Publisher'])
    player.permission_overrides={'puzzles.generate':False,'users.view':True,'unknown.test':True}
    assert not player.can('puzzles.generate') and player.can('users.view') and not player.can('unknown.test')
    player.roles=[roles['Player']]; player.permission_overrides={}
    register_permission('test_plugin.edit','Test plugin edit')
    register_route_permission('test_plugin_route','test_plugin.edit')
    assert not db.get(User,admin_id).can('test_plugin.edit')
    db.commit()
print('Migration preserves accounts, puzzles and progress; presets, union, deny precedence and plugin default-deny passed.')

base='http://127.0.0.1:8792'
def cookie(user_id):
    return TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({'user_id':user_id,'session_version':1}).encode())).decode()
server=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8792'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
try:
    with sync_playwright() as p:
        browser=p.chromium.launch()
        contexts={}
        for name,uid in {'Admin':admin_id,**ids}.items():
            context=browser.new_context(viewport={'width':1280,'height':1000})
            context.add_cookies([{'name':'session','value':cookie(uid),'url':base}]); contexts[name]=context
        admin,player,publisher,manager = [contexts[name] for name in ['Admin','Player','Publisher','Manager']]
        for _ in range(100):
            try:
                if admin.request.get(base+'/health').ok: break
            except Exception: time.sleep(.1)
        else: raise RuntimeError('QA server failed to start')
        def post(context,path,data): return context.request.post(base+path,data=urlencode(data,doseq=True),headers={'Content-Type':'application/x-www-form-urlencoded'},max_redirects=0)
        assert player.request.get(base+'/admin').status==403
        assert player.request.get(base+'/generate').status==403
        assert player.request.post(base+'/api/puzzles/generate',data={'puzzle_type':'takuzu'}).status==403
        assert player.request.get(base+'/admin/users').status==403
        assert post(player,f'/admin/users/{ids["Player"]}/permissions',{'roles':'1'}).status==403
        assert post(manager,'/admin/users',{'display_name':'Escalation','is_admin':'on'}).status==403
        assert publisher.request.get(base+'/admin/puzzles/mine/edit').ok
        assert publisher.request.get(base+'/admin/puzzles/private/edit').status==403
        assert publisher.request.get(base+'/puzzles/private').status==403
        assert publisher.request.get(base+'/api/puzzles/private').status==403
        assert publisher.request.get(base+'/api/puzzles/private/export').status==403
        assert post(publisher,'/admin/puzzles/private/archive',{}).status==403
        assert manager.request.get(base+'/admin/puzzles/private/edit').ok
        assert post(manager,'/admin/puzzles/mine/archive',{}).status==303
        assert player.request.get(base+'/puzzles/mine').status==403
        assert post(manager,'/admin/puzzles/mine/unarchive',{}).status==303
        assert player.request.get(base+'/puzzles/mine').ok
        assert manager.request.post(base+'/api/puzzles/mine/attempts',data={'answer':'test','user_id':ids['Player']}).status==403
        anonymous=browser.new_context()
        assert anonymous.request.get(base+'/puzzles/mine/print/render').status==401
        preview=publisher.request.get(base+'/api/puzzles/mine/print-preview')
        assert preview.ok,preview.text()
        parts=urlsplit(preview.json()['render_url'])
        render_path=parts.path+'?'+parts.query
        assert anonymous.request.get(base+render_path).ok
        assert anonymous.request.get(base+render_path.replace('/mine/','/private/')).status==401
        assert anonymous.request.get(base+render_path+'invalid').status==401
        assert post(player,'/admin/users/1/api-token',{}).status==403
        role_response=post(admin,'/admin/roles',{'name':'User editor','permissions':['users.view','users.edit','users.create','users.tokens']})
        assert role_response.status==303,role_response.text()
        with SessionLocal() as db:
            role=db.scalar(select(PermissionRole).where(PermissionRole.name=='User editor'))
            role_id=role.id
        assert post(admin,f'/admin/users/{ids["Player"]}/permissions',{'roles':str(role_id)}).status==303
        assert player.request.get(base+'/admin/users').ok
        assert post(player,'/admin/users',{'display_name':'Escalation','is_admin':'on'}).status==403
        invited_response=post(player,'/admin/users',{'display_name':'Invited','password':'Isolated-Password123!'})
        assert invited_response.status==303,(invited_response.status,invited_response.text()[:400])
        with SessionLocal() as db:
            invited=db.scalar(select(User).where(User.display_name=='Invited'))
            assert [r.name for r in invited.roles]==['Player'] and not invited.can('puzzles.generate')
        assert post(player,f'/admin/users/{admin_id}/api-token',{}).status==403
        assert post(player,f'/admin/users/{admin_id}',{'display_name':'admin','is_admin':'on'}).status==403
        assert post(player,f'/admin/users/{ids["Publisher"]}',{'display_name':'Publisher','is_active':'off'}).status==403
        own_issue=publisher.request.post(base+'/api/issues',data={'title':'Own draft','modules':[{'kind':'text','title':'Text','body':'Original'}]})
        other_issue=manager.request.post(base+'/api/issues',data={'title':'Other draft','modules':[{'kind':'text','title':'Text','body':'Other'}]})
        assert own_issue.ok and other_issue.ok
        own_issue_id,other_issue_id=own_issue.json()['id'],other_issue.json()['id']
        assert publisher.request.get(base+f'/admin/publishing/{other_issue_id}').status==403
        assert post(publisher,'/admin/publishing/save',{'issue_id':other_issue_id,'title':'Forbidden','modules_json':'[]'}).status==403
        # Role-granted generation and publishing remain separate, including manual editor writes.
        with SessionLocal() as db:
            publisher_role=db.scalar(select(PermissionRole).where(PermissionRole.name=='Publisher'))
            publisher_role_id=publisher_role.id
        assert post(admin,f'/admin/users/{ids["Publisher"]}/permissions',{'roles':str(publisher_role_id),'permission:puzzles.publish.own':'deny'}).status==303
        data={'puzzle_type':'takuzu','difficulty':'easy','generation_options':{'grid_size':4}}
        response=publisher.request.post(base+'/api/puzzles/generate',data=data)
        assert response.ok,response.text()
        pid=response.json()['id']
        with SessionLocal() as db:
            puzzle=db.get(Puzzle,pid)
            assert puzzle.puzzle_metadata['draft'] and puzzle.archived_at
            payload={'puzzle_type':'takuzu','title':'Blocked publication','print_payload':puzzle.print_payload,
                     'play_payload':puzzle.play_payload,'solution_payload':puzzle.solution_payload,'draft':False}
        assert publisher.request.post(base+'/api/puzzles/manual',data=payload).status==403
        assert post(publisher,f'/admin/puzzles/{pid}/publish',{}).status==403
        assert post(admin,f'/admin/users/{ids["Publisher"]}/permissions',{'roles':str(publisher_role_id),'permission:issues.publish.own':'deny'}).status==303
        assert publisher.request.post(base+f'/api/issues/{own_issue_id}/publish').status==403
        assert post(publisher,'/admin/publishing/save',{'issue_id':own_issue_id,'title':'Forbidden publish','action':'publish','modules_json':json.dumps([{'kind':'text','title':'Text','body':'Changed'}])}).status==403
        from app.models import PublishedIssue
        with SessionLocal() as db:
            assert db.get(PublishedIssue,own_issue_id).title=='Own draft'
        assert post(admin,f'/admin/users/{ids["Publisher"]}/permissions/reset',{}).status==303
        assert publisher.request.post(base+f'/api/issues/{own_issue_id}/publish').ok
        published=post(publisher,f'/admin/puzzles/{pid}/publish',{})
        assert published.status==303,(published.status,published.text()[:500])
        token=browser.new_context(extra_http_headers={'Authorization':'Bearer isolated-permission-token'})
        assert token.request.get(base+'/api/puzzles').ok
        rotated=post(admin,f'/admin/users/{ids["Publisher"]}/api-token',{})
        assert rotated.status==303
        rotated_token=parse_qs(urlsplit(rotated.headers['location']).query)['api_token'][0]
        assert token.request.get(base+'/api/puzzles').status==401
        token=browser.new_context(extra_http_headers={'Authorization':'Bearer '+rotated_token})
        assert token.request.get(base+'/api/puzzles').ok
        assert post(admin,f'/admin/users/{ids["Publisher"]}/permissions',{'roles':str(publisher_role_id),'permission:api.use':'deny'}).status==303
        assert token.request.get(base+'/api/puzzles').status==403
        assert publisher.request.get(base+'/api/puzzles').ok
        assert post(admin,f'/admin/users/{ids["Publisher"]}/permissions',{'roles':str(publisher_role_id),'permission:puzzles.print':'deny'}).status==303
        assert anonymous.request.get(base+render_path).status==403
        assert publisher.request.get(base+'/api/puzzles/mine/print-preview').status==403
        assert post(admin,f'/admin/users/{ids["Publisher"]}/permissions/reset',{}).status==303
        group=publisher.request.post(base+'/api/puzzles/mine/groups',data={})
        assert group.ok,group.text()
        gid=group.json()['id']
        assert publisher.request.get(base+f'/api/groups/{gid}/invite').ok
        assert post(admin,f'/admin/users/{ids["Publisher"]}/permissions',{'roles':str(publisher_role_id),'permission:groups.invite':'deny'}).status==303
        assert publisher.request.get(base+f'/api/groups/{gid}/invite').status==403
        with connect(base.replace('http:','ws:')+f'/api/groups/{gid}/presence/ws',origin=base,additional_headers={'Cookie':'session='+cookie(ids['Publisher'])},proxy=None) as ws:
            assert json.loads(ws.recv(timeout=3))['type']=='ready'
            assert post(admin,f'/admin/users/{ids["Publisher"]}/permissions',{'roles':str(publisher_role_id),'permission:groups.join':'deny'}).status==303
            try: ws.recv(timeout=6); raise AssertionError('Revoked socket remained connected')
            except ConnectionClosed as error: assert error.code==1008
        assert publisher.request.get(base+f'/api/groups/{gid}').status==403
        assert post(admin,f'/admin/users/{ids["Second admin"]}/permissions',{}).status==303
        assert post(admin,f'/admin/users/{admin_id}/permissions',{}).status==409
        assert post(admin,f'/admin/users/{admin_id}',{'display_name':'admin','is_active':'off'}).status==409
        with SessionLocal() as db:
            role=db.scalar(select(PermissionRole).where(PermissionRole.name=='Admin'))
            assert post(admin,f'/admin/roles/{role.id}',{'name':'Admin'}).status==409
            assert db.get(User,admin_id).can('roles.manage')
            assert db.scalar(select(PermissionAudit))
        # Account UI fixtures do not exercise scheduling. SQLite stores dates without timezone information.
        with SessionLocal() as db:
            for puzzle in db.scalars(select(Puzzle)):
                puzzle.auto_archive_at = None
            db.commit()
        solution_page = admin.new_page()
        solution_page.goto(base+'/puzzles/mine')
        assert solution_page.locator('#admin-solution').count() == 1
        assert not solution_page.locator('#admin-solution').evaluate('(panel) => panel.open')
        solution_page.locator('#admin-solution > summary').click()
        assert solution_page.locator('#admin-solution').evaluate('(panel) => panel.open')
        solution_page.reload()
        assert not solution_page.locator('#admin-solution').evaluate('(panel) => panel.open')
        solution_page.close()
        assert 'id="admin-solution"' not in publisher.request.get(base+'/puzzles/mine').text()
        for width in [1280,390]:
            page=admin.new_page()
            page.set_viewport_size({'width':width,'height':900})
            page.goto(base+f'/admin/users/{ids["Publisher"]}')
            assert page.locator('[data-permission]').count()>40
            assert page.locator('[data-user-panel="profile"]').is_visible()
            for tab in ['access','security','activity','profile']:
                page.locator(f'[data-user-tab="{tab}"]').click()
                assert page.locator(f'[data-user-panel="{tab}"]').is_visible()
                assert page.locator('[data-user-panel]:visible').count()==1
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.locator('[data-user-tab="profile"]').focus()
            page.keyboard.press('ArrowRight')
            assert page.locator('[data-user-tab="access"]').get_attribute('aria-selected')=='true'
            page.locator('[data-permission-search]').fill('generate')
            assert page.locator('.permission-row:visible').count()>0
            assert page.locator('.permission-row:visible').count()<page.locator('.permission-row').count()
            assert page.locator('[data-permission]').count()>40
            page.locator('[data-permission-search]').fill('')
            assert page.evaluate("[...new FormData(document.querySelector('[data-permission-form]')).keys()].filter(key => key.startsWith('permission:')).length") == page.locator('[data-permission]').count()
            page.screenshot(path=str(qa_output / f'user-access-{width}.png'),full_page=True)
            page.goto(base+f'/admin/users/{ids["Publisher"]}?tab=security')
            assert page.locator('[data-user-panel="security"]').is_visible()
            page.goto(base+f'/admin/users/{ids["Publisher"]}')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.screenshot(path=str(qa_output / f'user-{width}.png'),full_page=True)
            page.screenshot(path=str(qa_output / f'viewport-{width}.png'))
            page.goto(base+'/admin/users')
            assert page.locator('[data-user-card]').count()>=4
            page.locator('[data-user-search]').fill('Publisher')
            assert page.locator('[data-user-card]:visible').count()==1
            page.locator('[data-user-search]').fill('No such account')
            assert page.locator('[data-user-empty]').is_visible()
            page.locator('[data-user-search]').fill('')
            page.locator('[data-user-status]').select_option('disabled')
            assert page.locator('[data-user-card][data-status="enabled"]:visible').count()==0
            page.locator('[data-user-status]').select_option('all')
            page.locator('[data-create-user]').click()
            assert page.locator('[data-user-dialog]').is_visible()
            page.keyboard.press('Escape')
            assert not page.locator('[data-user-dialog]').is_visible()
            menu=page.locator('.user-menu').first
            menu.locator('summary').click()
            assert menu.locator('a').first.is_visible()
            page.keyboard.press('Escape')
            assert not menu.locator('a').first.is_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.screenshot(path=str(qa_output / f'users-{width}.png'),full_page=True)
            page.goto(base+'/admin/users?q=Publisher')
            assert page.locator('[data-user-card]:visible').count()==1
            page.locator('[data-user-search]').fill('')
            assert page.locator('[data-user-card]:visible').count()>=4
            page.close()
        assert admin.request.get(base+'/admin/roles').ok
        assert admin.request.get(base+'/admin/audit').ok
        assert manager.request.get(base+'/admin/audit').status==403
        assert admin.request.get(base+'/admin/plugins').ok
        assert admin.request.get(base+'/api/plugin-packages/catalog').ok
        assert manager.request.get(base+'/admin/plugins').status==403
        assert manager.request.get(base+'/api/plugin-packages').status==403
        from app.main import templates
        for template in templates.env.list_templates(): templates.env.get_template(template)
        browser.close()
    print('Web/API own-vs-any, creation/publish separation, privilege escalation, token and WebSocket revocation, last-admin guards and responsive user UI passed.')
    if engine.dialect.name=='postgresql':
        # Two simultaneous attempts to remove different admins must leave one administrator.
        with SessionLocal() as db:
            db.get(User,ids['Second admin']).roles=[db.scalar(select(PermissionRole).where(PermissionRole.name=='Admin'))]; db.commit()
        def demote(uid):
            with SessionLocal() as db:
                try:
                    lock_permissions(db)
                    db.get(User,uid).roles=[]
                    protect_last_admin(db); db.commit(); return True
                except HTTPException:
                    db.rollback(); return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(demote,[admin_id,ids['Second admin']]))
        assert sorted(results)==[False,True],results
        with SessionLocal() as db:
            assert sum(ADMIN_CORE <= effective_permissions(u) for u in db.scalars(select(User)))==1
            current=db.get(User,admin_id)
            before=effective_permissions(current)
            ensure_admin_user(db)
            assert effective_permissions(current)==before
        print('PostgreSQL concurrent last-admin protection and no bootstrap re-promotion passed.')
finally:
    server.terminate()
    try: server.wait(timeout=10)
    except subprocess.TimeoutExpired: server.kill(); server.wait()
    if server.returncode not in [0,-15]: print(server.stderr.read().decode()[-5000:])
