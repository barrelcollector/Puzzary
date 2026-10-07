"""Opt-in read-only deployment verification; no production puzzle fixtures."""
import os
if os.getenv('PUZZARY_SPELL_IT_LIVE_QA') != '1':
    raise SystemExit('Set PUZZARY_SPELL_IT_LIVE_QA=1 for read-only live verification')
import base64
import hashlib
import json
import sys
from pathlib import Path
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from app.database import Base, engine, SessionLocal
from app.models import User, WordBank
from app.main import SESSION_SECRET, word_bank_entries
from app.puzzles import get_puzzle_type

manifest=Path('/data/spell-it-deployment-check.json')
def fingerprint():
    result={}
    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            rows=sorted(json.dumps(dict(row),sort_keys=True,default=str) for row in connection.execute(select(table)).mappings())
            result[table.name]={'count':len(rows),'digest':hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
    return result
if '--snapshot' in sys.argv:
    with manifest.open('x') as handle: json.dump(fingerprint(),handle)
    print('Recorded all table counts/digests without database writes'); raise SystemExit()
if '--preflight' in sys.argv:
    with SessionLocal() as db:
        banks=db.scalars(select(WordBank).where(WordBank.archived_at.is_(None)).limit(10)).all()
        sources=[]
        for bank in banks: sources.extend(word_bank_entries(db,bank))
        game=get_puzzle_type('spell_it')
        eligible=game.source_builder(sources,'easy',set())
        assert eligible,'No live words with definitions available'
        built=game.generator('','easy',source=eligible,seed='read-only-preflight')
        assert built.play_payload['kind']=='spell_it'
    print('PASS: candidate generates from existing PostgreSQL banks without writes'); raise SystemExit()
before=json.loads(manifest.read_text())
assert before==fingerprint(),'Database changed during deployment; inspect before accepting'
with SessionLocal() as db:
    user=db.scalar(select(User).where(User.is_admin.is_(True),User.is_active.is_(True)))
    session=TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({'user_id':user.id,'session_version':user.session_version}).encode())).decode()
base='http://127.0.0.1:8790'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True); context=browser.new_context()
    context.add_cookies([{'name':'session','value':session,'url':base}])
    assert context.request.get(base+'/health').ok
    types=context.request.get(base+'/api/puzzle-types').json()
    records=types if isinstance(types,list) else types.get('items',types.get('puzzle_types',[]))
    assert any(item.get('slug',item.get('puzzle_type'))=='spell_it' for item in records),types
    errors=[]
    for width in [1280,390]:
        page=context.new_page(); page.set_viewport_size({'width':width,'height':900}); page.on('pageerror',lambda error:errors.append(str(error)))
        assert page.goto(base+'/admin/puzzles/manual').ok
        page.locator('[data-manual-puzzle-type]').select_option('spell_it')
        page.wait_for_function("document.querySelector('[data-level-editor=spell_it]') && document.querySelector('[data-level-toolbar] p[role=status]').textContent.includes('valid')")
        assert page.locator('.spell-editor-mask .spell-slot').count()==10
        assert page.locator('[name="manual_decoy_count"]').is_disabled()
        page.locator('[name="manual_letter_bank"]').select_option('decoy')
        assert page.locator('[name="manual_decoy_count"]').is_enabled()
        with page.expect_response(lambda response: '/spell-it/editor-preview' in response.url and response.request.method=='POST') as preview:
            page.locator('[name="manual_decoy_count"]').fill('7')
        entry=preview.value.json()['play_payload']['entries'][0]
        assert entry['settings']['letter_bank']=='decoy' and len(entry['bank'])==len(entry['hidden'])+7
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
        page.close()
    assert not errors,errors
    browser.close()
assert before==fingerprint(),'Read-only checks unexpectedly changed production data'
manifest.unlink()
print('PASS: live health, Spell It discovery, desktop/mobile editor preview; all table counts/digests unchanged')
