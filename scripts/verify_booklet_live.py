"""Read-only authenticated booklet UI/PDF and access-control smoke checks."""
import base64
from io import BytesIO
import json
from pathlib import Path
import os

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from pypdf import PdfReader
from sqlalchemy import select

from app.database import SessionLocal
from app.main import SESSION_SECRET
from app.models import PublishedIssue, User

output = Path(os.environ.get('QA_OUTPUT','/tmp/booklet-live'))
output.mkdir(parents=True,exist_ok=True)
base = 'http://127.0.0.1:8790'
with SessionLocal() as db:
    admin = db.scalar(select(User).where(User.is_admin == True,User.is_active == True).order_by(User.id))
    issue = db.scalar(select(PublishedIssue).where(PublishedIssue.published_at.is_not(None),PublishedIssue.archived_at.is_(None)).order_by(PublishedIssue.created_at.desc()))
    assert issue, 'A published live issue is required for this read-only check'
    issue_id = issue.id
    snapshot = json.dumps(issue.snapshot,sort_keys=True)
    payload = base64.b64encode(json.dumps({'user_id':admin.id,'session_version':admin.session_version or 1}).encode())
    cookie = TimestampSigner(SESSION_SECRET).sign(payload).decode()

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={'width':1280,'height':900})
    context.add_cookies([{'name':'session','value':cookie,'url':base}])
    page = context.new_page()
    errors = []
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(base+f'/admin/publishing/{issue_id}')
    page.get_by_role('link',name='Booklet / PDF').click()
    assert page.locator('[name="print_mode"]').input_value() == 'booklet'
    assert page.locator('[name="width_mm"]').input_value() == '215.9'
    assert page.locator('[name="height_mm"]').input_value() == '279.4'
    assert page.get_by_role('link',name='Download booklet PDF').is_visible()
    page.screenshot(path=str(output/'booklet-toolbar-desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(output/'booklet-toolbar-mobile.png'),full_page=True)
    plain = context.request.get(base+f'/issues/{issue_id}/print.pdf?size=letter')
    assert plain.ok, plain.text()
    pages = len(PdfReader(BytesIO(plain.body())).pages)
    for size in ('letter','a4'):
        result = context.request.get(base+f'/issues/{issue_id}/print.pdf?size={size}&print_mode=booklet')
        assert result.ok, result.text()
        assert '-booklet.pdf' in result.headers['content-disposition']
        pdf = PdfReader(BytesIO(result.body()))
        assert len(pdf.pages) == ((pages+3)//4)*2
        assert all(float(side.mediabox.width) > float(side.mediabox.height) for side in pdf.pages)
        (output/f'live-booklet-{size}.pdf').write_bytes(result.body())
    assert context.request.get(base+f'/issues/{issue_id}/print.pdf?print_mode=booklet&size=zebra').status == 422
    assert context.request.get(base+f'/issues/{issue_id}/print.pdf?print_mode=invalid').status == 422
    anonymous = browser.new_context()
    denied = anonymous.request.get(base+f'/api/issues/{issue_id}/print.pdf?print_mode=booklet')
    assert denied.status == 401
    denied = anonymous.request.get(base+f'/issues/{issue_id}/print.pdf?print_mode=booklet',max_redirects=0)
    assert denied.status in (302,303,307)
    assert not errors, errors
    browser.close()
with SessionLocal() as db:
    assert json.dumps(db.get(PublishedIssue,issue_id).snapshot,sort_keys=True) == snapshot
print('Live booklet toolbar, Letter/A4 PDF, roll rejection, auth and immutable snapshot checks passed.')
