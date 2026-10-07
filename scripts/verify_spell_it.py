"""Isolated Spell It engine, authorization, persistence and browser checks."""
import os
os.environ['DATABASE_URL'] = os.getenv('SPELL_IT_QA_DATABASE', 'sqlite:////tmp/puzzary-spell-it-qa.sqlite')
os.environ['SESSION_SECRET'] = 'isolated-spell-it-secret'
os.environ['UPLOAD_DIR'] = '/tmp/spell-it-uploads'

import base64
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from pypdf import PdfReader
from sqlalchemy import select
from app.database import Base, engine, SessionLocal
from app.models import User, Puzzle, PuzzleAttempt, DictionaryWord, DictionaryDefinition, WordBank, WordBankWord, PermissionRole
from app.permissions import initialize_permissions
from app.main import SESSION_SECRET, hash_password
from app.puzzle_types import spell_it as game


def rejects(call):
    try: call()
    except (ValueError, TypeError): return
    raise AssertionError('Invalid entry accepted')


items = [{'answer':'BALLOON', 'definition':'An inflatable bag.', 'hidden':[1,2,3,4,5]},
         {'answer':'ICE-CREAM!', 'definition':'A frozen dessert.', 'hidden':[1,2,4,5,6,7]}]
for difficulty, percent in game.DIFFICULTIES.items():
    built = game.assemble([{'answer':'ASTRONOMER', 'definition':'A scientist studying stars.'}], {}, difficulty, '', 'qa')
    entry = built.solution_payload['entries'][0]
    assert entry['settings']['mask_percent'] == percent
    assert 0 not in entry['hidden'] and 9 not in entry['hidden']
    assert len(entry['hidden']) == max(1, int(8*percent/100+.5))
for answer in ['A','AT','CAT','ICE-CREAM!']:
    e = game.manual({'entries':[{'answer':answer,'definition':'QA definition.'}]}).solution_payload['entries'][0]
    assert e['hidden'] and all(answer[p].isalpha() for p in e['hidden'])
for mode in ['off','exact','decoy','hint_triggered']:
    opts = {'entries':items,'letter_bank':mode,'seed':'repeatable'}
    a, b = game.manual(opts), game.manual(opts)
    assert a == b
    for e, public in zip(a.solution_payload['entries'], a.play_payload['entries']):
        assert 'answer' not in public
        assert all(public['slots'][p] is None for p in e['hidden'])
        missing = Counter(e['answer'][p] for p in e['hidden'])
        assert not missing - Counter(e['bank'])
        assert len(e['bank']) == len(e['hidden']) + (3 if mode == 'decoy' else 0)
        assert bool(public['bank']) == (mode in ('exact','decoy'))
for count in [0,1,7,20]:
    overridden = game.manual({'entries':[{**items[0],'difficulty':'hard'}],'letter_bank':'decoy','decoy_count':count})
    entry=overridden.solution_payload['entries'][0]
    assert entry['settings']['letter_bank']=='decoy' and len(entry['bank'])==len(entry['hidden'])+count
    assert sum(ch not in entry['answer'] for ch in entry['bank'])==count
    assert len(overridden.play_payload['entries'][0]['bank'])==len(entry['bank'])
assert game.manual({'entries':[{**items[0],'difficulty':'hard'}]}).solution_payload['entries'][0]['settings']['letter_bank']=='off'
assert game.manual({'entries':[{**items[0],'difficulty':'hard'}],'letter_bank':'exact'}).solution_payload['entries'][0]['settings']['letter_bank']=='exact'
for entry in [{'answer':'CAT'}, {'answer':'CAT','definition':'x','hidden':[]}, {'answer':'CAT','definition':'x','hidden':[1,1]}, {'answer':'CAT','definition':'x','hidden':[99]}, {'answer':'---','definition':'x'}]:
    rejects(lambda: game.manual({'entries':[entry]}))
rejects(lambda: game.manual({'entries':items,'mask_percent':20.5}))
rejects(lambda: game.generate('', 'easy', source=items[:1], generation_options={'mode':'series','entry_count':2}))
variety = {tuple(game.manual({'entries':[{'answer':'ASTRONOMER','definition':'Star scientist.'}],'seed':i}).solution_payload['entries'][0]['hidden']) for i in range(20)}
assert len(variety) > 1
assert game.check_answer('ice-cream!', game.manual({'entries':[items[1]]}).solution_payload)[0]
assert not game.check_answer('ice cream', game.manual({'entries':[items[1]]}).solution_payload)[0]
solution = game.manual({'entries':items,'letter_bank':'off'}).solution_payload
forged = game.merge_progress(SimpleNamespace(answers={'hint:0':'1,2,3','bank:0':'1'}), solution, {})
assert forged == {'answers':{}}
saved = game.merge_progress(SimpleNamespace(answers={}), solution, {'answers':{'hint:0':'1','0:1':'A'}})
assert saved['answers']['hint:0']=='1' and saved['answers']['0:1']=='A'
print('PASS: deterministic masks, difficulty, phrases, repeated banks, validation and hint integrity', flush=True)

Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
with SessionLocal() as db:
    admin = User(display_name='admin',is_admin=True,is_active=True,password_hash=hash_password('isolated-spell-password'))
    db.add(admin); db.commit(); initialize_permissions(db)
    player = User(display_name='Speller',is_active=True,permissions_initialized=True,roles=[db.scalar(select(PermissionRole).where(PermissionRole.name=='Player'))])
    db.add(player); db.flush(); ids={'admin':admin.id,'player':player.id}
    for name, options in [('series',{'entries':items,'letter_bank':'exact'}), ('hint',{'entries':items[:1],'letter_bank':'hint_triggered','hint_limit':1}), ('off',{'entries':items[:1],'letter_bank':'off'}), ('private',{'entries':items[:1]}), ('decoy',{'entries':items[:1],'letter_bank':'decoy','decoy_count':7})]:
        generated=game.manual(options)
        db.add(Puzzle(id=name,puzzle_type='spell_it',title=f'Spell {name}',theme='',difficulty='easy',created_by_id=admin.id,
            prompt=generated.prompt,print_payload=generated.print_payload,play_payload=generated.play_payload,solution_payload=generated.solution_payload,
            puzzle_metadata={**generated.metadata,'draft':name=='private','manual_editor_options':options},source='test'))
    bank=WordBank(name='Spell QA',slug='spell-qa',description='Definitions for Spell It',payload={'mode':'manual'})
    for i, item in enumerate(items):
        word=DictionaryWord(word=item['answer'].replace('-','').replace('!',''),display_text=item['answer'],definitions=[DictionaryDefinition(definition=item['definition'])])
        bank.word_links.append(WordBankWord(word=word,definition=word.definitions[0],position=i))
    db.add(bank); db.commit()

base='http://127.0.0.1:8795'
output=Path('/tmp/spell-it-qa'); output.mkdir(exist_ok=True)
def cookie(uid): return TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({'user_id':uid,'session_version':1}).encode())).decode()
server=subprocess.Popen(['uvicorn','app.main:app','--host','127.0.0.1','--port','8795'],stdout=open(output/'server.log','w'),stderr=subprocess.STDOUT)
try:
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        admin=browser.new_context(); player=browser.new_context()
        for ctx,name in [(admin,'admin'),(player,'player')]: ctx.add_cookies([{'name':'session','value':cookie(ids[name]),'url':base}])
        for _ in range(100):
            try:
                if admin.request.get(base+'/health').ok: break
            except Exception: pass
            time.sleep(.1)
        else: raise AssertionError((output/'server.log').read_text())
        endpoint=base+'/api/puzzles/'
        assert browser.new_context().request.get(endpoint+'series/spell-it/progress').status==401
        assert player.request.post(base+'/api/spell-it/editor-preview',data={'entries':items}).status==403
        assert player.request.get(endpoint+'private/spell-it/progress').status==403
        preview=admin.request.post(base+'/api/spell-it/editor-preview',data={'entries':items})
        assert preview.ok,preview.text()
        public=player.request.get(endpoint+'series').json()
        assert 'manual_editor_options' not in public.get('metadata',{})
        assert 'BALLOON' not in json.dumps(public)
        answers={f'0:{i}':items[0]['answer'][i] for i in items[0]['hidden']}
        checked=player.request.post(endpoint+'series/spell-it/attempts',data={'answers':answers}).json()
        assert checked['states']==['correct','incomplete'] and not checked['completed']
        with SessionLocal() as db: assert not db.scalar(select(PuzzleAttempt).where(PuzzleAttempt.puzzle_id=='series')).is_correct
        answers.update({f'1:{i}':items[1]['answer'][i] for i in items[1]['hidden']})
        assert player.request.post(endpoint+'series/spell-it/attempts',data={'answers':answers}).json()['completed']
        player.request.post(endpoint+'series/reset',data={})
        # The generic progress API cannot mint hints or reset consumed quotas.
        assert player.request.post(endpoint+'hint/progress',data={'answers':{'hint:0':'1,2','bank:0':'1'}}).json()['answers']=={}
        def concurrent_hint(_):
            req=Request(endpoint+'hint/spell-it/hint',data=b'{"entry":0,"kind":"letter"}',headers={'Cookie':'session='+cookie(ids['player']),'Content-Type':'application/json'})
            try:
                with urlopen(req) as response: return response.status
            except HTTPError as error: return error.code
        if engine.dialect.name=='postgresql':
            with ThreadPoolExecutor(max_workers=2) as pool: statuses=list(pool.map(concurrent_hint,range(2)))
            assert sorted(statuses)==[200,400],statuses
            hint=player.request.get(endpoint+'hint/spell-it/progress').json()
        else: hint=player.request.post(endpoint+'hint/spell-it/hint',data={'entry':0,'kind':'letter','answers':{}}).json()
        assert hint['hints_remaining']==[0]
        player.request.post(endpoint+'hint/progress',data={'answers':{}})
        assert player.request.get(endpoint+'hint/spell-it/progress').json()['hints_remaining']==[0]
        assert player.request.post(endpoint+'hint/spell-it/hint',data={'entry':0,'kind':'letter'}).status==400
        bank=player.request.post(endpoint+'hint/spell-it/hint',data={'entry':0,'kind':'bank'}).json()
        assert Counter(bank['letter_banks'][0])==Counter('ALLOO')
        player.request.post(endpoint+'hint/reset',data={})
        assert player.request.get(endpoint+'hint/spell-it/progress').json()['hints_remaining']==[1]
        generated=admin.request.post(base+'/api/puzzles/generate',data={'puzzle_type':'spell_it','word_bank_slugs':['spell-qa'],'difficulty':'easy','seed':'qa','generation_options':{'mode':'series','entry_count':2}})
        assert generated.ok,generated.text()
        # SQLite QA only: avoid unrelated existing naive/aware dashboard date behavior.
        if engine.dialect.name=='sqlite':
            with SessionLocal() as db:
                for puzzle in db.scalars(select(Puzzle)): puzzle.auto_archive_at=None
                db.commit()
        print('PASS: authenticated generation, private answers, series checks, reveal quotas and reset',flush=True)
        errors=[]
        wizard=admin.new_page(); wizard.on('pageerror',lambda error:errors.append(str(error)))
        wizard.goto(base+'/generate'); wizard.locator('[data-puzzle-type]').select_option('spell_it')
        wizard.locator('[data-wizard-next]').click(); wizard.locator('[name="word_bank_slugs"]').select_option('spell-qa'); wizard.locator('[data-wizard-next]').click()
        assert wizard.locator('[name="gen_decoy_count"]').is_disabled()
        wizard.locator('[name="gen_letter_bank"]').select_option('decoy'); assert wizard.locator('[name="gen_decoy_count"]').is_enabled()
        wizard.locator('[name="gen_decoy_count"]').fill('7'); wizard.locator('[data-wizard-next]').click()
        assert wizard.locator('[data-generation-summary]').inner_text().find('Extra letters (Decoy bank)')>=0
        with wizard.expect_navigation(): wizard.locator('[data-wizard-submit]').click()
        wizard.wait_for_selector('.spell-slot.blank:not([disabled])')
        public=admin.request.get(endpoint+wizard.url.split('/')[-1]).json()['play_payload']['entries'][0]
        assert public['settings']['decoy_count']==7 and len(public['bank'])==len(public['hidden'])+7
        assert wizard.locator('.spell-bank-tile').count()==len(public['bank'])
        print('PASS: decoy wizard controls and requested extra letters survive generation and web play',flush=True)
        wizard.close()
        page=player.new_page(); page.on('pageerror',lambda error:errors.append(str(error)))
        for width in [1280,390]:
            page.set_viewport_size({'width':width,'height':900})
            page.goto(base+'/puzzles/series'); page.wait_for_selector('.spell-slot.blank:not([disabled])')
            assert page.locator('details.admin-solution').count()==0
            assert page.get_by_role('button',name='Next Word',exact=True).is_disabled()
            page.get_by_role('button',name='Check Word',exact=True).click()
            page.wait_for_function("document.querySelector('.spell-status').textContent.includes('missing letters')")
            first=page.locator('input[data-position="1"]'); first.fill('Z')
            assert first.input_value()==''
            first.focus(); page.keyboard.type('ALLOO')
            assert page.locator('input[data-position="5"]').input_value()=='O'
            assert page.locator('.spell-bank-tile:not([disabled])').count()==0
            first.focus(); page.keyboard.press('Backspace')
            assert page.locator('.spell-bank-tile:not([disabled])').count()==1
            page.locator('.spell-bank-tile:not([disabled])').click()
            assert first.input_value()=='A'
            page.get_by_role('button',name='Check Word',exact=True).click()
            page.wait_for_function("document.querySelector('.spell-status').textContent==='Correct.'")
            page.get_by_role('button',name='Next Word',exact=True).click()
            page.wait_for_function("document.querySelector('.spell-stage h2').textContent.includes('Word 2')")
            for position in items[1]['hidden']: page.locator(f'input[data-position="{position}"]').fill(items[1]['answer'][position])
            page.wait_for_timeout(500)
            page.reload(); page.wait_for_selector('.spell-slot.blank:not([disabled])')
            assert page.locator('.spell-stage h2').inner_text()=='Word 2 of 2'
            assert page.locator('input[data-position="1"]').input_value()=='C'
            page.screenshot(path=str(output/f'play-{width}.png'),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            with page.expect_response(lambda response: '/spell-it/attempts' in response.url and response.request.method=='POST'):
                page.get_by_role('button',name='Check Word',exact=True).click()
            page.wait_for_function("document.querySelector('.spell-status').textContent==='Spell It complete.'")
            player.request.post(endpoint+'series/reset',data={})
        page.goto(base+'/puzzles/off'); page.wait_for_selector('.spell-slot.blank:not([disabled])')
        for position in items[0]['hidden']: page.locator(f'input[data-position="{position}"]').fill('X')
        page.get_by_role('button',name='Check Word',exact=True).click()
        page.wait_for_function("document.querySelector('.spell-status').textContent.includes('Not quite')")
        page.goto(base+'/puzzles/hint'); page.wait_for_selector('.spell-slot.blank:not([disabled])')
        page.get_by_role('button',name='Show Letter Bank',exact=True).click()
        page.wait_for_selector('.spell-bank-tile')
        page.get_by_role('button',name='Reveal Letter (1)',exact=True).click()
        page.wait_for_selector('input[readonly]'); page.reload(); page.wait_for_selector('input[readonly]')
        assert page.get_by_role('button',name='Reveal Letter (0)',exact=True).is_disabled()
        try:
            with page.expect_navigation(timeout=5000): page.get_by_role('button',name='Reset',exact=True).click()
        except Exception:
            raise AssertionError({'errors':errors,'status':page.locator('.spell-status').inner_text(),'reset':page.get_by_role('button',name='Reset',exact=True).evaluate('el => ({html:el.outerHTML, form:el.form.outerHTML, valid:el.form.checkValidity()})')})
        page.wait_for_selector('.spell-slot.blank:not([disabled])')
        assert page.locator('input[readonly]').count()==0
        assert page.get_by_role('button',name='Reveal Letter (1)',exact=True).is_enabled()
        assert page.locator('.spell-slots').get_attribute('aria-label')=='Spelling'
        assert all(label.startswith('Word 1, letter ') for label in page.locator('input.spell-slot').evaluate_all('els => els.map(el => el.getAttribute("aria-label"))'))
        print('PASS: desktop/mobile typing, repeated tiles, replacement, sequential completion, reload and hints',flush=True)
        editor=admin.new_page(); editor.on('pageerror',lambda error:errors.append(str(error)))
        editor.goto(base+'/admin/puzzles/manual'); editor.locator('[data-manual-puzzle-type]').select_option('spell_it')
        editor.wait_for_function("document.querySelector('.spell-editor-entry') && document.querySelector('[data-level-toolbar] p[role=status]').textContent.includes('valid')")
        assert editor.locator('[name="manual_decoy_count"]').is_disabled()
        editor.locator('[name="manual_letter_bank"]').select_option('decoy')
        assert editor.locator('[name="manual_decoy_count"]').is_enabled()
        editor.locator('[name="manual_decoy_count"]').fill('7')
        editor.locator('.spell-editor-entry details').first.locator('summary').click()
        with editor.expect_response(lambda response: '/spell-it/editor-preview' in response.url and response.request.method=='POST') as preview_result:
            editor.locator('.spell-editor-entry details select').first.select_option('hard')
        preview_entry=preview_result.value.json()['play_payload']['entries'][0]
        assert preview_entry['settings']['letter_bank']=='decoy' and len(preview_entry['bank'])==len(preview_entry['hidden'])+7
        editor.get_by_label('Word bank',exact=True).select_option(label='Spell QA')
        editor.wait_for_function("document.querySelector('[data-level-toolbar] select').value && document.querySelectorAll('[data-level-toolbar] select')[1].options.length > 1")
        editor.get_by_label('Word and definition',exact=True).select_option(index=1)
        editor.get_by_role('button',name='Add Selected Word',exact=True).click()
        assert editor.locator('.spell-editor-entry').count()==2
        editor.get_by_role('button',name='Remove word 2',exact=True).click()
        assert editor.locator('.spell-editor-entry').count()==1
        editor.locator('.spell-editor-mask button').first.click()
        editor.wait_for_timeout(500)
        editor.get_by_role('button',name='Add Word',exact=True).click()
        row=editor.locator('.spell-editor-entry').nth(1)
        row.locator('[data-field="answer"]').fill('ICE-CREAM!'); row.locator('[data-field="definition"]').fill('A frozen dessert.')
        editor.wait_for_timeout(600)
        row.locator('.spell-position').fill('1'); row.locator('.spell-position').dispatch_event('change')
        editor.wait_for_timeout(600)
        assert editor.locator('.spell-editor-entry').first.locator('[data-field="answer"]').input_value()=='ICE-CREAM!'
        editor.locator('select[name="draft"]').select_option('false')
        editor.screenshot(path=str(output/'editor-desktop.png'),full_page=True)
        editor.set_viewport_size({'width':390,'height':844}); editor.screenshot(path=str(output/'editor-mobile.png'),full_page=True)
        assert editor.evaluate('document.documentElement.scrollWidth <= innerWidth')
        editor.get_by_role('button',name='Save',exact=True).click(); editor.wait_for_url('**/admin/puzzles/*/edit?*')
        editor.wait_for_selector('.spell-editor-entry')
        assert editor.locator('.spell-editor-entry').count()==2
        puzzle_id=editor.url.split('/')[-2]
        printable=admin.new_page(); printable.goto(base+f'/puzzles/{puzzle_id}/print?size=letter'); printable.emulate_media(media='print')
        assert printable.locator('.spell-print-entry').count()==2
        assert printable.locator('.spell-print-entry .blank').count()>0
        printable.screenshot(path=str(output/'print-letter.png'),full_page=True)
        for size in ['letter','a4','receipt']:
            response=admin.request.get(base+f'/api/puzzles/{puzzle_id}/print.pdf?size={size}')
            assert response.ok,response.text()
            pdf=PdfReader(BytesIO(response.body())); assert len(pdf.pages)==1
            if size=='letter': assert abs(float(pdf.pages[0].mediabox.width)-612)<2 and abs(float(pdf.pages[0].mediabox.height)-792)<2
            (output/f'spell-it-{size}.pdf').write_bytes(response.body())
        assert not errors,errors
        print('PASS: visual mask editing, entry reordering, publishing, responsive editor and print rendering; no JS errors',flush=True)
        browser.close()
finally:
    server.terminate(); server.wait(timeout=15)
