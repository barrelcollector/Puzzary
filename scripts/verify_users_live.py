"""Opt-in, read-only checks of the deployed account management UI."""
import os

if os.getenv('PUZZARY_USERS_LIVE_QA') != '1':
    raise SystemExit('Set PUZZARY_USERS_LIVE_QA=1 to run read-only live UI checks')

import base64
import json
from pathlib import Path
import tempfile
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from app.database import SessionLocal
from app.main import SESSION_SECRET
from app.models import User

with SessionLocal() as db:
    user = next(u for u in db.scalars(select(User)) if u.is_active and u.can('roles.manage') and u.can('users.view'))
    uid = user.id
    cookie = TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({'user_id': uid, 'session_version': user.session_version}).encode())).decode()

base = 'http://127.0.0.1:8790'
output = Path(tempfile.gettempdir()) / 'users-live-qa'
output.mkdir(exist_ok=True)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
    context = browser.new_context()
    context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
    errors = []
    for width in [1280, 390]:
        page = context.new_page()
        page.set_viewport_size({'width': width, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        assert page.goto(base + '/admin/users').ok
        assert page.locator('[data-user-card]').count() > 0
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=str(output / f'users-{width}.png'), full_page=True)
        page.locator('[data-create-user]').click()
        assert page.locator('[data-user-dialog]').is_visible()
        page.keyboard.press('Escape')
        assert not page.locator('[data-user-dialog]').is_visible()
        assert page.goto(base + f'/admin/users/{uid}').ok
        for tab in ['profile', 'access', 'security', 'activity']:
            page.locator(f'[data-user-tab="{tab}"]').click()
            assert page.locator(f'[data-user-panel="{tab}"]').is_visible()
            assert page.locator('[data-user-panel]:visible').count() == 1
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.screenshot(path=str(output / f'{tab}-{width}.png'), full_page=True)
        page.close()
    for asset in ['users.js?v=1', 'styles.css?v=34', 'icons/plus.svg', 'icons/ellipsis-vertical.svg', 'icons/x.svg']:
        assert context.request.get(base + '/static/' + asset).ok, asset
    assert not errors, errors
    browser.close()
print('Live user grid, dialog, all account tabs, responsive layout and local assets passed without browser errors. No accounts or permissions changed.')
