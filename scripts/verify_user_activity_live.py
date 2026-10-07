"""Opt-in read-only activity preflight and deployed account/library checks."""
import os
if os.getenv('PUZZARY_ACTIVITY_LIVE_QA') != '1':
    raise SystemExit('Set PUZZARY_ACTIVITY_LIVE_QA=1 for read-only live checks')

import base64
import json
import sys
from pathlib import Path
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from app.database import SessionLocal
from app.main import SESSION_SECRET
from app.models import User
from app.user_activity import account_activity

with SessionLocal() as db:
    accounts=[]
    admin_id=None
    for user in db.scalars(select(User).where(User.is_active.is_(True))):
        activity=account_activity(db,user)
        assert activity['user_id']==user.id
        assert activity['totals']['played']==activity['totals']['completed']+activity['totals']['in_progress']
        assert all(sum(row[key] for row in activity['by_type'])==value for key,value in activity['totals'].items())
        if user.can('users.view') and user.can('activity.view'):
            admin_id=user.id
        if user.can('library.view'):
            cookie=TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({'user_id':user.id,'session_version':user.session_version}).encode())).decode()
            accounts.append((user.id,cookie,user.can('library.archived_view')))
if '--preflight' in sys.argv:
    print('Read-only preflight passed for existing account activity and plugin-normalized progress.')
    raise SystemExit(0)

base='http://127.0.0.1:8790'
output=Path('/tmp/activity-live-qa'); output.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
    errors=[]
    for uid,cookie,archive_access in accounts:
        context=browser.new_context()
        context.add_cookies([{'name':'session','value':cookie,'url':base}])
        response=context.request.get(base+'/api/account/stats')
        assert response.ok,(uid,response.status)
        stats=response.json()
        assert stats['user_id']==uid
        if not archive_access:
            response=context.request.get(base+'/api/puzzles?availability=archived')
            assert response.ok and not response.json()['items']
        for width in [1280,390]:
            page=context.new_page(); page.set_viewport_size({'width':width,'height':900})
            page.on('pageerror',lambda error: errors.append(str(error)))
            assert page.goto(base+'/account').ok
            assert int(page.locator('[data-stat="completed"]').inner_text())==stats['totals']['completed']
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            if uid==admin_id:
                page.screenshot(path=str(output/f'account-{width}.png'),full_page=True)
                assert page.goto(base+f'/admin/users/{uid}?tab=activity').ok
                assert page.locator('[data-account-stats]').is_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            assert page.goto(base+'/library').ok
            if uid==admin_id:
                page.screenshot(path=str(output/f'library-{width}.png'),full_page=True)
            page.close()
        context.close()
    assert not errors,errors
    browser.close()
print('Live account-scoped stats/API, archive visibility and desktop/mobile account/library views passed. No progress or account settings changed.')
