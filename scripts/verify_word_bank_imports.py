"""Isolated import/export, source security, permissions and browser regression checks."""
import os
os.environ['DATABASE_URL'] = os.getenv('BANK_IMPORT_QA_DATABASE', 'sqlite:////tmp/puzzary-bank-import-qa.sqlite')
os.environ['SESSION_SECRET'] = 'isolated-bank-import-secret'
os.environ['UPLOAD_DIR'] = '/tmp/bank-import-uploads'

import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import func, select
from app.database import Base, engine, SessionLocal
from app.models import DictionaryWord, DictionaryDefinition, User, WordBank, WordBankWord, PermissionRole, Puzzle
from app.permissions import initialize_permissions
from app.main import hash_password, hash_secret, SESSION_SECRET, word_bank_entries
from app.puzzles import get_puzzle_type
from app.word_bank_imports import parse_sources, preview, mapped_entry
from app.word_bank_sources import checked_url, source_addresses, fetch, fetch_sources

if os.getenv('BANK_IMPORT_FETCH_ONLY') == '1':
    request=json.load(sys.stdin)
    sources=fetch_sources(request['source_url'],request.get('api_token',''))
    assert sources and all(source['text'] for source in sources)
    print('PASS: authenticated configured source fetched; credentials not retained')
    raise SystemExit()


def rejects(function):
    try:
        function()
    except (ValueError, TypeError):
        return
    raise AssertionError('Unsafe or invalid input accepted')


for url in ['file:///etc/passwd', 'http://user:password@example.com/file', 'http://example.com:22/file', 'https://example.com/file?token=secret']:
    rejects(lambda: checked_url(url))
for address in ['127.0.0.1', '169.254.169.254', '192.168.1.20', '::1', '::ffff:127.0.0.1']:
    with patch('app.word_bank_sources.socket.getaddrinfo', return_value=[(2,1,6,'',(address,443))]):
        rejects(lambda: source_addresses(checked_url('https://example.com/words')))
with patch.dict(os.environ, {'WORD_BANK_IMPORT_ALLOWED_HOSTS':'example.com'}), patch('app.word_bank_sources.socket.getaddrinfo', return_value=[(2,1,6,'',('192.168.1.20',443))]):
    assert source_addresses(checked_url('https://example.com/words')) == ['192.168.1.20']
with patch('app.word_bank_sources.socket.getaddrinfo', return_value=[(2,1,6,'',('8.8.8.8',443)),(2,1,6,'',('127.0.0.1',443))]):
    rejects(lambda: source_addresses(checked_url('https://example.com/words')))

class MockResponse:
    def __init__(self, status, headers=None, body=b'CAT'):
        self.status, self.headers, self.body = status, headers or {}, body
    def getheader(self, key, default=None): return self.headers.get(key, default)
    def read(self, limit): return self.body[:limit]

class MockConnection:
    responses, requests = [], []
    def __init__(self, parts, address): self.parts = parts
    def request(self, method, path, headers): self.requests.append((self.parts.hostname, headers))
    def getresponse(self): return self.responses.pop(0)
    def close(self): pass

with patch('app.word_bank_sources.PinnedConnection', MockConnection), patch('app.word_bank_sources.source_addresses', return_value=['8.8.8.8']):
    MockConnection.responses = [MockResponse(302, {'Location':'https://other.example/file'}), MockResponse(200)]
    assert fetch('https://example.com/file', 'mock-token')['text'] == 'CAT'
    assert MockConnection.requests[-2][1]['Authorization'] == 'Bearer mock-token'
    assert 'Authorization' not in MockConnection.requests[-1][1]
    MockConnection.responses = [MockResponse(200, {'Content-Length':'2000001'})]
    rejects(lambda: fetch('https://example.com/file'))
    MockConnection.responses = [MockResponse(200, {'Content-Encoding':'gzip'})]
    rejects(lambda: fetch('https://example.com/file'))
with patch('app.word_bank_sources.PinnedConnection', MockConnection):
    MockConnection.responses = [MockResponse(302, {'Location':'http://127.0.0.1/private'})]
    def resolve(parts):
        if parts.hostname == '127.0.0.1': raise ValueError('Private redirect')
        return ['8.8.8.8']
    with patch('app.word_bank_sources.source_addresses', side_effect=resolve):
        rejects(lambda: fetch('https://example.com/file'))

calls=[]
def mock_source(url, token='', budget=None):
    calls.append(url)
    if '/repos/' in url and '/git/trees/' not in url: data={'default_branch':'main'}
    elif '/git/trees/' in url: data={'tree':[{'path':'words.txt','type':'blob'}]}
    elif '/gists/' in url: data={'files':{'words.txt':{'filename':'words.txt','raw_url':'https://gist.githubusercontent.com/user/id/raw/words.txt'}}}
    else: return {'url':url,'text':'CAT\nDOG','content_type':'text/plain'}
    return {'url':url,'text':json.dumps(data),'content_type':'application/json'}
with patch('app.word_bank_sources.fetch', side_effect=mock_source):
    for url in ['https://github.com/example/vocab','https://github.com/example/vocab/blob/main/words.txt','https://gist.github.com/user/id','https://pastebin.com/id','https://other.example']:
        assert fetch_sources(url)[0]['text']=='CAT\nDOG'
    assert 'https://other.example/api/word-banks/export' in calls
    assert 'https://pastebin.com/raw/id' in calls
if os.getenv('BANK_IMPORT_PUBLIC_SOURCE_QA') == '1':
    remote=fetch_sources('https://raw.githubusercontent.com/python/cpython/main/README.rst')
    assert 'python' in remote[0]['text'].lower()
    print('PASS: real public GitHub TLS/raw source fetch')

rejects(lambda: parse_sources([{'text':json.dumps(['CAT']*5001)}]))
rejects(lambda: parse_sources([{'text':'x'*2000001}]))
rejects(lambda: parse_sources([{'text':'['*1500+'0'+']'*1500}]))
rejects(lambda: mapped_entry({'word':'caf\u00e9'}, {'word':'word'}))
rejects(lambda: mapped_entry({'word':'cat\x00'}, {'word':'word'}))

Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
with SessionLocal() as db:
    admin=User(display_name='admin',is_admin=True,is_active=True,password_hash=hash_password('isolated-bank-password'),api_token_hash=hash_secret('isolated-bank-token'))
    db.add(admin); db.commit(); initialize_permissions(db)
    player=User(display_name='Bank Player',is_active=True,roles=[db.scalar(select(PermissionRole).where(PermissionRole.name=='Player'))],permissions_initialized=True)
    other=User(display_name='Bank Editor',is_active=True,permissions_initialized=True,permission_overrides={'content.view':True,'content.manage':True,'api.use':True})
    word=DictionaryWord(word='CLOUD',display_text='Local cloud',clue='Local clue',properties={'local_note':'keep'},definitions=[DictionaryDefinition(definition='Visible water droplets.',part_of_speech='Noun',position=0,properties={})])
    banned=DictionaryWord(word='bannedqa',display_text='bannedqa',banned_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc))
    picker=DictionaryWord(word='pickerqa',display_text='Picker QA',definitions=[
        DictionaryDefinition(definition='First selectable meaning.',part_of_speech='Noun',position=0),
        DictionaryDefinition(definition='Second selectable meaning.',part_of_speech='Verb',position=1),
    ])
    db.add_all([player,other,word,banned,picker]); db.commit()
    ids={'admin':admin.id,'player':player.id,'other':other.id}

base='http://127.0.0.1:8794'
def cookie(uid):
    return TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({'user_id':uid,'session_version':1}).encode())).decode()
output=Path('/tmp/bank-import-qa'); output.mkdir(exist_ok=True)
server=subprocess.Popen(['uvicorn','app.main:app','--host','127.0.0.1','--port','8794'],stdout=open(output/'server.log','w'),stderr=subprocess.STDOUT)
try:
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        contexts={}
        for name,uid in ids.items():
            ctx=browser.new_context(); ctx.add_cookies([{'name':'session','value':cookie(uid),'url':base}]); contexts[name]=ctx
        admin,player,other=[contexts[name] for name in ['admin','player','other']]
        for _ in range(100):
            try:
                if admin.request.get(base+'/health').ok: break
            except Exception: time.sleep(.1)
        else: raise AssertionError((output/'server.log').read_text())
        endpoint=base+'/api/word-banks/import/'
        source={'name':'Weather QA','source_text':json.dumps({'schema':'puzzary.word-bank.v1','bank':{'license':'CC0','language':'en','attribution':'QA author'},'entries':[
            {'word':'cloud','display_text':'Remote cloud','definitions':[{'definition':'Visible water droplets.','part_of_speech':'Noun'},{'definition':'Remote computing infrastructure.','part_of_speech':'Noun','clues':['Available on the internet.'],'tags':[{'name':'Technology','tag_type':'Theme'}]}], 'selected_definition':1,'properties':{'difficulty':'easy','plugin_extension':{'value':1}}},
            {'word':'cloud','definitions':[{'definition':'Visible water droplets.','part_of_speech':'Noun'}]},
            {'word':'fogqa','definition':'Water suspended near the ground.','tags':'Weather','language':'en'},
            {'word':'bannedqa'}, {'word':'invalid\u00e9'}]})}
        assert player.request.post(endpoint+'preview',data=source).status==403
        assert browser.new_context().request.post(endpoint+'preview',data=source).status==401
        response=admin.request.post(endpoint+'preview',data=source); assert response.ok,response.text()
        data=response.json(); assert data['valid']==4 and len(data['errors'])==1
        key=data['preview_id']
        assert other.request.post(endpoint+'commit',data={'preview_id':key}).status==404
        assert admin.request.post(endpoint+'commit',data={'preview_id':key}).status==422
        response=admin.request.post(endpoint+'commit',data={'preview_id':key,'skip_invalid':True}); assert response.ok,response.text()
        result=response.json(); bank_id=result['bank_id']
        assert result['added']==1 and result['definitions_added']==2 and result['duplicate_definitions']==2,result
        assert admin.request.post(endpoint+'commit',data={'preview_id':key}).status==404
        with SessionLocal() as db:
            words=db.scalars(select(DictionaryWord).where(func.lower(DictionaryWord.word)=='cloud')).all()
            assert len(words)==1 and words[0].display_text=='Local cloud' and len(words[0].definitions)==2
            bank=db.get(WordBank,bank_id); assert len(bank.word_links)==3
            assert bank.payload['properties']['license']=='CC0' and bank.payload['properties']['imports'][0]['importer_id']==ids['admin']
            entries=word_bank_entries(db,bank); assert len(entries)==2
            selected=next(e for e in entries if e['word'].word.lower()=='cloud')
            assert selected['definition'].definition=='Remote computing infrastructure.'
            assert selected['properties']['difficulty']=='easy'
            built=get_puzzle_type('crossword').source_builder(entries,'easy',set())
            assert any('Available on the internet.' in e['clues'] for e in built),built
            assert get_puzzle_type('word_search').source_builder(entries,'easy',set())
        export=admin.request.get(base+f'/api/word-banks/{bank_id}/export'); assert export.ok,export.text()
        exported=export.json(); assert exported['schema']=='puzzary.word-bank.v1' and len(exported['entries'])==2
        source2={'source_text':json.dumps(exported),'bank_id':bank_id}
        preview2=admin.request.post(endpoint+'preview',data=source2).json()
        response=admin.request.post(endpoint+'commit',data={'preview_id':preview2['preview_id']}); assert response.ok,response.text()
        assert response.json()['definitions_added']==0 and response.json()['linked']==0
        update={'name':'Update QA','source_text':'word,definition\ncloud,An updated supplied sense.'}
        key=admin.request.post(endpoint+'preview',data=update).json()['preview_id']
        assert admin.request.post(endpoint+'commit',data={'preview_id':key,'policy':'update'}).ok
        key=admin.request.post(endpoint+'preview',data=update).json()['preview_id']
        assert admin.request.post(endpoint+'commit',data={'preview_id':key,'policy':'skip'}).json()['skipped']==1
        mapped={'name':'Mapped QA','format':'csv','source_text':'Vocabulary,Meaning,Labels\nmappingqa,A mapped entry.,Weather','mapping':{'word':'Vocabulary','definition':'Meaning','tags':'Labels'}}
        response=admin.request.post(endpoint+'preview',data=mapped); assert response.ok,response.text()
        assert response.json()['entries'][0]['definitions'][0]['definition']=='A mapped entry.'
        assert admin.request.post(endpoint+'commit',data={'preview_id':response.json()['preview_id']}).ok
        with SessionLocal() as db:
            bank=db.get(WordBank,bank_id); slug=bank.slug
        generated=admin.request.post(base+'/api/puzzles/generate',data={'puzzle_type':'word_search','word_bank_slugs':[slug],'difficulty':'easy','seed':'import-generation-fixture','generation_options':{'grid_size':'10','word_count_min':'1','word_count_limit':'2'}})
        assert generated.ok,generated.text()
        assert set(generated.json()['print_payload']['words']) <= {'CLOUD','FOGQA'},generated.json()
        assert generated.json()['print_payload']['words']
        # SQLite drops timezone information; the existing dashboard assumes PostgreSQL datetimes.
        if engine.dialect.name == 'sqlite':
            with SessionLocal() as db:
                db.get(Puzzle,generated.json()['id']).auto_archive_at=None
                db.commit()
        token=browser.new_context(extra_http_headers={'Authorization':'Bearer isolated-bank-token'})
        assert token.request.get(base+f'/api/word-banks/{bank_id}/export').ok
        assert 'word_bank_required_fields' in admin.request.get(base+'/api/puzzle-types').json()['puzzle_types'][0]
        picker_endpoint=base+f'/api/word-banks/{bank_id}/entry-options'
        assert browser.new_context().request.get(picker_endpoint+'?q=picker').status==401
        assert player.request.get(picker_endpoint+'?q=picker').status==403
        assert admin.request.get(picker_endpoint+'?q=p').json()['items']==[]
        picker_results=admin.request.get(picker_endpoint+'?q=second%20selectable').json()['items']
        assert any(item['definition']=='Verb: Second selectable meaning.' for item in picker_results)
        errors=[]
        for width in [1280,390]:
            page=admin.new_page(); page.set_viewport_size({'width':width,'height':900}); page.on('pageerror',lambda error:errors.append(str(error)))
            assert page.goto(base+'/admin/word-banks/import').ok
            page.screenshot(path=str(output/f'source-{width}.png'),full_page=True)
            page.locator('input[name=file]').set_input_files({'name':'vocabulary.csv','mimeType':'text/csv','buffer':b'Vocabulary,Meaning\ncanvasqa,A surface for drawing.\nletterqa,A written symbol.'})
            page.get_by_role('button',name='Continue',exact=True).click()
            page.locator('input[name=name]').fill(f'Browser QA {width}')
            page.locator('select[name=map_word]').select_option('Vocabulary')
            page.locator('select[name=map_definition]').select_option('Meaning')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.screenshot(path=str(output/f'mapping-{width}.png'),full_page=True)
            page.get_by_role('button',name='Preview',exact=True).click()
            assert page.get_by_text('A surface for drawing.',exact=True).count()==1
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.screenshot(path=str(output/f'review-{width}.png'),full_page=True)
            page.get_by_role('button',name='Import',exact=True).click()
            assert page.get_by_role('heading',name='Import Complete').count()==1
            page.get_by_role('link',name='Open Word Bank').click()
            assert page.get_by_role('link',name='Export',exact=True).count()==1
            assert page.locator('select[name="word_definition"]').count()==0
            search=page.locator('[data-picker-search]')
            picker_options=page.locator('[data-picker-results] [role="option"]')
            search.fill('Picker QA')
            picker_options.first.wait_for()
            assert picker_options.count()==3
            if width==1280:
                search.press('ArrowDown'); search.press('ArrowDown'); search.press('ArrowDown'); search.press('Enter')
            else:
                picker_options.filter(has_text='Second selectable meaning.').click()
            assert not page.locator('[data-picker-submit]').is_disabled()
            assert page.locator('[data-picker-status]').inner_text().startswith('Selected Picker QA')
            page.locator('[data-picker-submit]').click()
            assert page.get_by_text('Second selectable meaning.',exact=True).count()==1
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.close()
        with SessionLocal() as db:
            picker_word=db.scalar(select(DictionaryWord).where(DictionaryWord.word=='pickerqa'))
            browser_banks=db.scalars(select(WordBank).where(WordBank.name.startswith('Browser QA'))).all()
            assert len(browser_banks)==2
            assert all(any(link.word_id==picker_word.id and link.definition.position==1 for link in bank.word_links) for bank in browser_banks)
        assert not errors,errors
        browser.close()
    print('PASS: sources/SSRF/limits, definition merge and searchable bank selection, portable roundtrip, policies/mapping, account permissions/ownership/replay, desktop/mobile upload wizard')
finally:
    server.terminate(); server.wait(timeout=10)
