"""Opt-in read-only live verification; never imports or creates production words."""
import os
if os.getenv('PUZZARY_BANK_IMPORT_LIVE_QA') != '1':
    raise SystemExit('Set PUZZARY_BANK_IMPORT_LIVE_QA=1 only for read-only live verification')
import base64
import hashlib
import json
import sys
from pathlib import Path
import tempfile
from urllib.request import Request, urlopen
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from app.database import Base, engine, SessionLocal
from app.models import User, WordBank
from app.main import SESSION_SECRET
from app.word_bank_imports import parse_sources, portable_bank

manifest=Path('/data/word-bank-import-check.json')
def fingerprint():
    result={}
    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            rows=[dict(row) for row in connection.execute(select(table)).mappings()]
            serialized=sorted(json.dumps(row,sort_keys=True,default=str) for row in rows)
            result[table.name]={'count':len(rows),'digest':hashlib.sha256('\n'.join(serialized).encode()).hexdigest()}
    return result

if '--snapshot' in sys.argv:
    with manifest.open('x') as handle: json.dump(fingerprint(),handle)
    print('Recorded table counts and digests; no database writes')
    raise SystemExit()
if '--preflight' in sys.argv:
    with SessionLocal() as db:
        banks=db.scalars(select(WordBank).where(WordBank.archived_at.is_(None)).limit(5)).all()
        for bank in banks:
            payload=portable_bank(db,bank)
            if payload['entries']:
                parsed=parse_sources([{'text':json.dumps(payload),'url':'preflight-export'}])
                assert len(parsed['rows'])==len(payload['entries'])
        assert db.scalar(select(User).where(User.is_admin.is_(True)))
    print('PASS: candidate loads existing PostgreSQL banks and exports without writes')
    raise SystemExit()

before=json.loads(manifest.read_text())
after=fingerprint()
assert before==after, {name:{'before':before.get(name),'after':after.get(name)} for name in set(before)|set(after) if before.get(name)!=after.get(name)}
with SessionLocal() as db:
    admin=db.scalar(select(User).where(User.is_admin.is_(True),User.is_active.is_(True)))
    session=TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({'user_id':admin.id,'session_version':admin.session_version}).encode())).decode()
    bank=db.scalar(select(WordBank).where(WordBank.archived_at.is_(None)))
    paths=['/health','/admin/word-banks','/admin/word-banks/import','/api/word-banks/export','/api/puzzle-types']
    if bank: paths.extend([f'/admin/word-banks/{bank.id}',f'/api/word-banks/{bank.id}/export'])
    for path in paths:
        with urlopen(Request('http://127.0.0.1:8790'+path,headers={'Cookie':'session='+session}),timeout=30) as response:
            body=response.read().decode()
            assert response.status==200 and 'Internal Server Error' not in body,path
            if path=='/admin/word-banks': assert '/admin/word-banks/import' in body
            if path=='/admin/word-banks/import': assert 'Source API token' in body and 'Import Word Bank' in body
            if path.endswith('/export'): assert json.loads(body)['schema'].startswith('puzzary.word-bank.')
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context()
        context.add_cookies([{'name':'session','value':session,'url':'http://127.0.0.1:8790'}])
        output=Path(tempfile.gettempdir()) / 'live-bank-import'; output.mkdir(exist_ok=True)
        for width in [1280,390]:
            page=context.new_page(); page.set_viewport_size({'width':width,'height':900})
            assert page.goto('http://127.0.0.1:8790/admin/word-banks/import').ok
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            assert page.locator('input[name=api_token]').get_attribute('type')=='password'
            page.screenshot(path=str(output/f'source-{width}.png'),full_page=True)
            page.close()
        browser.close()
manifest.unlink()
print('PASS: all table counts/digests unchanged; authenticated live import page, exports, banks and plugin contracts respond successfully')
