"""Offline browser regressions for protected brushes and row confirmation."""
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import sync_playwright
from starlette.templating import Jinja2Templates

templates = Jinja2Templates(directory='/app/app/templates')
grid = [[1, 0, 1, 0, 1], [0, 1, 0, 1, 0], [1, 0, 0, 0, 1],
        [0, 0, 1, 0, 0], [0, 0, 0, 0, 0]]
payload = {'size':5, 'row_clues':[[1,1,1],[1,1],[1,1],[1],[0]],
           'column_clues':[[1,1],[1],[1,1],[1],[1,1]], 'givens':{},
           'settings':{}}

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width':800,'height':900})
    def load(auto=True, saved=None, columns=True):
        puzzle = SimpleNamespace(id='test', prompt='', play_payload={**payload, 'settings':{'auto_confirm_rows':auto, 'auto_confirm_columns':columns}}, solution_payload={'grid':grid})
        html = templates.env.get_template('puzzle_types/nonogram.html').render(puzzle=puzzle, progress_answers=saved or {})
        page.route('**/*', lambda route: route.abort())
        page.set_content(html + '<button id="reveal-solution">Reveal</button>')
        page.evaluate('''window.fetch=async()=>({ok:true}); window.celebrations=0; window.tones=0; window.peaks=[];
            window.PuzzaryCelebrate={complete:()=>{window.celebrations++}};
            window.muted=false;
            Object.defineProperty(window,'localStorage',{configurable:true,value:{getItem:()=>window.muted ? 'true' : 'false'}});
            window.AudioContext=class {
              currentTime=0; destination={}; resume(){return Promise.resolve()}
              createOscillator(){return {frequency:{},connect(g){return g},start(){window.tones++},stop(){}}}
              createGain(){return {gain:{setValueAtTime(){},exponentialRampToValueAtTime(v){window.peaks.push(v)}},connect(){}}}
            }; void 0;''')
        page.add_script_tag(content=Path('/app/app/static/celebration.js').read_text())
        page.add_script_tag(content=Path('/app/app/static/nonogram.js').read_text())
    def cell(x,y=0): return page.locator(f'button.nonogram-cell[data-key="{x},{y}"]')
    def mode(value): page.locator(f'[data-mode="{value}"]').click()
    def stroke(start, end, button='left'):
        a=cell(*start).bounding_box(); b=cell(*end).bounding_box()
        page.mouse.move(a['x']+a['width']/2,a['y']+a['height']/2)
        page.mouse.down(button=button)
        page.mouse.move(b['x']+b['width']/2,b['y']+b['height']/2,steps=30)
        page.mouse.up(button=button)
    def values(y=0): return [cell(x,y).get_attribute('data-value') for x in range(5)]
    def confirmed(y=0): return page.locator('tbody tr').nth(y).locator('th').get_attribute('class') == 'confirmed'

    load()
    mode('0'); cell(1).click(); cell(3).click()
    mode('1'); stroke((0,0),(4,0))
    assert values() == ['1','0','1','0','1']
    assert confirmed()
    # A Fill stroke starting on a filled cell erases fill marks, never X marks.
    stroke((0,0),(4,0))
    assert values() == ['', '0', '', '0', '']
    assert not confirmed()
    # A Fill stroke may start on an X and still paint beyond it.
    stroke((1,0),(4,0))
    assert values() == ['', '0', '1', '0', '1']
    cell(0).click()
    assert confirmed()
    # Empty strokes starting on an X erase X marks only, not fills.
    mode('0'); stroke((1,0),(4,0))
    assert values() == ['1','','1','','1']
    assert confirmed()
    # Empty strokes may start on a filled cell and paint neighboring blanks.
    stroke((0,0),(4,0))
    assert values() == ['1','0','1','0','1']
    cell(0).press(' ')
    assert cell(0).get_attribute('data-value') == '1'
    mode('1'); cell(1).press(' ')
    assert cell(1).get_attribute('data-value') == '0'
    cell(0).click(button='right')
    assert cell(0).get_attribute('data-value') == '1'
    mode(''); stroke((0,0),(4,0))
    assert values() == ['']*5
    assert not confirmed()
    # Correct runs placed at the wrong coordinates must not be confirmed.
    mode('1'); cell(1,2).click(); cell(3,2).click()
    assert not confirmed(2)
    # Confirmation is restored from saved answers and disabled by the setting.
    saved={f'{x},0':str(v) for x,v in enumerate(grid[0])}
    load(saved=saved); assert confirmed()
    load(auto=False,saved=saved); assert not confirmed()
    load()
    mode('0'); stroke((0,4),(4,4))
    assert confirmed(4)
    page.locator('#reveal-solution').click()
    assert values() == ['1','0','1','0','1']
    page.locator('.nonogram-clear').click()
    assert values() == ['']*5
    # Columns confirm independently; each transition plays one quiet two-note chime.
    load(auto=False)
    cell(0,0).click(); cell(0,2).click()
    assert page.locator('thead th[scope="col"]').first.get_attribute('class') == 'confirmed'
    assert page.evaluate('window.tones') == 2
    assert page.evaluate('Math.max(...window.peaks)') == 0.05
    cell(1,1).click()
    assert page.evaluate('window.tones') == 4
    cell(1,1).press('x')  # Protected fill does not replay the chime.
    assert page.evaluate('window.tones') == 4
    cell(0,0).press('Delete')
    assert page.locator('thead th[scope="col"]').first.get_attribute('class') == ''
    load(auto=False,columns=False)
    cell(0,0).click(); cell(0,2).click()
    assert page.evaluate('window.tones') == 0
    load()
    page.evaluate('window.muted=true')
    for x in (0,2,4): cell(x).click()
    assert confirmed() and page.evaluate('window.tones') == 0
    # Filling the solution completes without Check or explicit white-cell marks.
    load()
    for y,row in enumerate(grid):
        for x,value in enumerate(row):
            if value: cell(x,y).click()
    assert page.locator('.nonogram-status').inner_text() == 'Nonogram complete.'
    assert page.evaluate('window.celebrations') == 1
    mode('0'); cell(0).click()
    assert page.evaluate('window.celebrations') == 1
    cell(0).press('Delete')
    assert page.locator('.nonogram-status').inner_text() == ''
    mode('1'); cell(0).click()
    assert page.evaluate('window.celebrations') == 2
    solved={f'{x},{y}':str(v) for y,row in enumerate(grid) for x,v in enumerate(row)}
    load(saved=solved)
    assert page.evaluate('window.tones == 0 && window.celebrations == 0')
    assert page.locator('.nonogram-status').inner_text() == 'Nonogram complete.'
    # Shared sounds work without a celebration panel, for any module.
    page.evaluate('window.PuzzarySounds.smallWin()')
    assert page.evaluate('window.tones') == 3
    assert page.evaluate('Math.max(...window.peaks)') == 0.06
    page.evaluate('window.muted=true; window.PuzzarySounds.progress()')
    assert page.evaluate('window.tones') == 3
    page.evaluate('window.muted=false; window.PuzzarySounds.complete()')
    assert page.evaluate('window.tones') == 6
    assert page.evaluate('Math.max(...window.peaks)') == 0.08
    page.evaluate('window.AudioContext=undefined; window.webkitAudioContext=undefined; window.PuzzarySounds.progress()')
    assert page.evaluate('window.tones') == 6
    # Touch taps use the same protected painting path.
    touch=browser.new_context(has_touch=True,viewport={'width':390,'height':844})
    phone=touch.new_page()
    puzzle=SimpleNamespace(id='test',prompt='',play_payload={**payload,'settings':{'auto_confirm_rows':True}},solution_payload={'grid':grid})
    html=templates.env.get_template('puzzle_types/nonogram.html').render(puzzle=puzzle,progress_answers={})
    phone.route('**/*',lambda route: route.abort())
    phone.set_content(html)
    phone.evaluate('window.fetch=async()=>({ok:true})')
    phone.add_script_tag(content=Path('/app/app/static/nonogram.js').read_text())
    c=phone.locator('button.nonogram-cell').first
    c.tap(); phone.locator('[data-mode="0"]').tap(); c.tap()
    assert c.get_attribute('data-value') == '1'
    phone.locator('[data-mode=""]').tap(); c.tap()
    assert c.get_attribute('data-value') == ''
    c.tap()  # Erase keeps an empty cell empty.
    assert c.get_attribute('data-value') == ''
    browser.close()
print('Protected Fill/Empty strokes, stroke starts, toggled strokes, erasing, keyboard, touch, reveal, row confirmation On/Off/reload: passed')
