"""Offline browser interactions, confirmation, hints, and Takuzu canvas tests."""
from pathlib import Path
from types import SimpleNamespace
import json
import os

from playwright.sync_api import sync_playwright
from starlette.templating import Jinja2Templates

grid=[[0,0,1,1],[0,1,0,1],[1,0,1,0],[1,1,0,0]]
payload={'size':4,'givens':{'3,3':'0'},'settings':{'auto_confirm_lines':True}}
plugin=Path(os.getenv("PUZZARY_PLUGIN_DIR", "/data/plugins"))/'takuzu'
templates=Jinja2Templates([plugin/'templates', Path('/app/app/templates')])
static=plugin/'static'
with sync_playwright() as p:
    browser=p.chromium.launch()
    page=browser.new_page()
    errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
    def load(auto=True,saved=None):
        page.goto('about:blank')
        puzzle=SimpleNamespace(id='test',prompt='',play_payload={**payload,'settings':{'auto_confirm_lines':auto}},solution_payload={'grid':grid})
        page.route('**/*',lambda route:route.abort())
        html=templates.env.get_template('puzzle_types/takuzu.html').render(puzzle=puzzle,progress_answers=saved or {})
        page.set_content(html+'<button id="reveal-solution">Reveal</button>')
        page.evaluate('window.fetch=async()=>({ok:true}); window.chimes=0; window.wins=0')
        page.add_script_tag(content=Path('/app/app/static/celebration.js').read_text())
        page.evaluate('window.PuzzarySounds.progress=()=>{window.chimes++}; window.PuzzaryCelebrate={complete:()=>{window.wins++}}')
        page.add_script_tag(content=(static/'takuzu.js').read_text())
    def cell(x,y=0): return page.locator(f'button.takuzu-cell[data-key="{x},{y}"]')
    load()
    cell(0).click(); assert cell(0).inner_text()=='0'
    cell(0).click(); assert cell(0).inner_text()=='1'
    cell(0).click(); assert cell(0).inner_text()==''
    for x,v in enumerate(grid[0]): cell(x).press(str(v))
    assert page.locator('tbody tr').first.locator('th').get_attribute('class')=='confirmed'
    assert page.evaluate('window.chimes')==1
    page.locator('.takuzu-check').click(); assert page.evaluate('window.chimes')==1
    cell(0).press('Delete'); assert page.locator('tbody tr').first.locator('th').get_attribute('class')==''
    cell(0).press('1'); page.locator('.takuzu-check').click()
    assert '1 incorrect' in page.locator('.takuzu-status').inner_text()
    page.locator('.takuzu-hint').click(); assert cell(0).inner_text()=='0'
    assert page.evaluate('window.chimes')==1
    page.locator('.takuzu-clear').click()
    assert cell(3,3).inner_text()=='0' and cell(3,3).is_disabled()
    page.locator('[data-mode="1"]').click()
    a=cell(0,1).bounding_box(); b=cell(3,1).bounding_box()
    page.mouse.move(a['x']+a['width']/2,a['y']+a['height']/2); page.mouse.down()
    page.mouse.move(b['x']+b['width']/2,b['y']+b['height']/2,steps=20); page.mouse.up()
    assert all(cell(x,1).inner_text()=='1' for x in range(4))
    page.locator('.takuzu-clear').click()
    for y,row in enumerate(grid):
        for x,v in enumerate(row):
            if cell(x,y).is_enabled(): cell(x,y).press(str(v))
    assert page.locator('.takuzu-status').inner_text()=='Takuzu complete.'
    assert page.evaluate('window.wins')==1
    saved={f'{x},{y}':str(v) for y,row in enumerate(grid) for x,v in enumerate(row)}
    load(saved=saved); assert page.evaluate('window.wins==0 && window.chimes==0')
    load(auto=False)
    for x,v in enumerate(grid[0]): cell(x).press(str(v))
    assert page.evaluate('window.chimes')==0
    page.locator('.takuzu-check').click(); assert page.evaluate('window.chimes')==1
    load(); page.locator('#reveal-solution').click()
    assert page.evaluate('window.wins==0 && window.chimes==0')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    box=cell(0).bounding_box(); assert abs(box['width']-box['height'])<1
    # Editor paints zero/one and synchronizes the JSON source without saving a puzzle.
    page.goto('about:blank')
    page.set_content('<form><label><textarea name="manual_solution_grid"></textarea></label></form><div id="tools"></div><div id="canvas"></div>')
    page.add_script_tag(content=(static/'takuzu-editor.js').read_text())
    page.evaluate("window.PuzzaryManualEditors.takuzu(document.querySelector('form'),{toolbar:document.querySelector('#tools'),canvas:document.querySelector('#canvas')})")
    assert 'Valid' in page.locator('[role="status"]').inner_text()
    page.locator('#canvas button[data-key="0,0"]').click()
    assert json.loads(page.locator('textarea').input_value())[0][0]==1
    assert 'violations' in page.locator('[role="status"]').inner_text()
    assert not errors,errors
    browser.close()
print('Takuzu cycle, keyboard, drag, givens, errors, hints, completion, reload, sound modes and canvas checks passed.')
