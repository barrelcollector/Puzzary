"""Offline browser checks for progress sounds in each applicable puzzle module."""
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

static = Path('/app/app/static')
plugins = Path(os.getenv("PUZZARY_PLUGIN_DIR", "/data/plugins"))


def attr(value):
    return json.dumps(value).replace("'", '&#39;')


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def load(html, script):
        page.goto('about:blank')
        page.set_content(html + '<button id="reveal-solution">Reveal</button>')
        page.evaluate('window.fetch=async()=>({ok:true})')
        page.add_script_tag(content=(static / 'celebration.js').read_text())
        page.evaluate('''window.chimes=0; window.wins=0;
            window.PuzzarySounds.progress=()=>{window.chimes++};
            window.PuzzaryCelebrate={complete:()=>{window.wins++}};''')
        slug = {
            'crossword.js': 'crossword', 'sudoku.js': 'sudoku',
            'cross-equations.js': 'cross_equations', 'word-pyramid.js': 'word_pyramid',
            'word-search.js': 'word_search',
        }[script]
        page.add_script_tag(content=(plugins / slug / 'static' / script).read_text())
        assert count() == 0

    def count():
        return page.evaluate('window.chimes')

    def fill(key, value):
        page.locator(f'input[data-key="{key}"]').fill(value)

    def board(kind, cell_class, solution, size, extra='', givens=None):
        givens = givens or {}
        cells = ''.join(f'<label class="{cell_class}"><input data-key="{key}" value="{givens.get(key, "")}"></label>' for key in solution)
        return f'''<div class="{kind}-play" data-solution='{attr(solution if kind == 'cross-equations' else [[solution[f'{x},{y}'] for x in range(size)] for y in range(size)])}' data-size="{size}" data-grid-size="{size}" {extra}>{cells}</div>
            <p id="{kind}-status"></p><button id="{kind}-check">Check</button><button id="{kind}-clear">Clear</button>'''

    # Crossword: checked entries, auto-highlight, repeat checks, edits, reveal, reload.
    solution = {'0,0':'A', '1,0':'B', '0,1':'C', '1,1':'D'}
    entries = [{'number':1,'direction':'across','x':0,'y':0,'answer':'AB'}, {'number':2,'direction':'across','x':0,'y':1,'answer':'CD'}]
    def crossword(auto=False, givens=None):
        html = board('crossword', 'crossword-cell', solution, 2, f"data-entries='{attr(entries)}' data-settings='{attr({'auto_highlight_correct':auto})}'", givens)
        html += '<button class="check-entry" data-entry-number="1" data-entry-direction="across">Check word</button>'
        load(html, 'crossword.js')
    crossword()
    fill('0,0','A'); fill('1,0','B')
    assert count() == 0
    page.locator('.check-entry').click(); assert count() == 1
    page.locator('#crossword-check').click(); assert count() == 1
    fill('0,0','Z'); fill('0,0','A')
    page.locator('.check-entry').click(); assert count() == 2
    fill('0,1','C'); fill('1,1','D')
    page.locator('#crossword-check').click(); assert count() == 2
    assert page.evaluate('window.wins') == 1
    crossword(auto=True)
    fill('0,0','A'); fill('1,0','B'); page.wait_for_timeout(50)
    assert count() == 1
    page.locator('#reveal-solution').click()
    page.locator('#crossword-check').click(); assert count() == 1
    crossword(givens={'0,0':'A','1,0':'B'})
    page.locator('#crossword-check').click(); assert count() == 0

    # Sudoku: row, column and box milestones only on Check, including 4x4 boxes.
    grid = [[1,2,3,4],[3,4,1,2],[2,1,4,3],[4,3,2,1]]
    solution = {f'{x},{y}':str(value) for y,row in enumerate(grid) for x,value in enumerate(row)}
    def sudoku(mode='check_puzzle'):
        load(board('sudoku','sudoku-cell',solution,4,f'data-check-mode="{mode}" data-box-rows="2" data-box-cols="2"'), 'sudoku.js')
    sudoku()
    for x in range(4): fill(f'{x},0',solution[f'{x},0'])
    assert count() == 0
    page.locator('#sudoku-check').click(); assert count() == 1
    page.locator('#sudoku-check').click(); assert count() == 1
    fill('0,1','3'); fill('1,1','4')
    page.locator('#sudoku-check').click(); assert count() == 2
    page.locator('#reveal-solution').click()
    page.locator('#sudoku-check').click(); assert count() == 2
    sudoku('none')
    for x in range(4): fill(f'{x},0',solution[f'{x},0'])
    page.locator('#sudoku-check').click(); assert count() == 0

    # Cross Equations: confirmed equations, including zero-valued answers.
    solution = {f'{x*2},{y*2}':str(x+y) for y in range(3) for x in range(3)}
    equations = [{'cells':[f'{x*2},{y*2}' for x in range(3)]} for y in range(3)]
    load(board('cross-equations','number-cell',solution,3,f"data-equations='{attr(equations)}'"), 'cross-equations.js')
    for x in range(3): fill(f'{x*2},0',str(x))
    assert count() == 0
    page.locator('#cross-equations-check').click(); assert count() == 1
    page.locator('#cross-equations-check').click(); assert count() == 1
    page.locator('#reveal-solution').click()
    page.locator('#cross-equations-check').click(); assert count() == 1

    # Word Pyramid: confirmed rows, not individual letters or revealed rows.
    rows = [{'answer':'AB'},{'answer':'ABC'},{'answer':'ABCD'}]
    html = f"<div class='word-pyramid-play' data-solution='{attr(rows)}'>"
    for y,row in enumerate(rows):
        html += '<div class="word-pyramid-row">' + ''.join(f'<input data-row="{y}" data-cell="{x}">' for x in range(len(row['answer']))) + '</div>'
    html += '</div><p id="word-pyramid-status"></p><button id="word-pyramid-check">Check</button><button class="reveal-pyramid-row" data-row="1">Reveal row</button>'
    load(html,'word-pyramid.js')
    for x,letter in enumerate('AB'): page.locator(f'input[data-row="0"][data-cell="{x}"]').fill(letter)
    assert count() == 0
    page.locator('#word-pyramid-check').click(); assert count() == 1
    page.locator('#word-pyramid-check').click(); assert count() == 1
    page.locator('.reveal-pyramid-row').click()
    page.locator('#word-pyramid-check').click(); assert count() == 1

    # Word Search: auto-confirm and manual checks use real pointer strokes.
    placements = [{'word':'AB','start':[0,0],'direction':[1,0]}, {'word':'CD','start':[0,1],'direction':[1,0]}]
    def search(auto=True):
        html = f'''<div class="word-grid interactive" data-words="AB,CD" data-auto-confirm="{str(auto).lower()}" data-placements='{attr(placements)}' style="display:grid;grid-template-columns:50px 50px;width:100px">'''
        for y,row in enumerate(['AB','CD']):
            for x,letter in enumerate(row): html += f'<button data-letter="{letter}" data-x="{x}" data-y="{y}" style="height:50px">{letter}</button>'
        html += '</div><div id="word-list"><span data-word="AB">AB</span><span data-word="CD">CD</span></div><p id="word-search-status"></p><button id="word-search-check">Check</button>'
        load(html,'word-search.js')
    def stroke(y):
        a=page.locator(f'button[data-x="0"][data-y="{y}"]').bounding_box()
        b=page.locator(f'button[data-x="1"][data-y="{y}"]').bounding_box()
        page.mouse.move(a['x']+25,a['y']+25); page.mouse.down()
        page.mouse.move(b['x']+25,b['y']+25,steps=10); page.mouse.up()
    search(); stroke(0); assert count() == 1
    stroke(0); assert count() == 1
    stroke(1); assert count() == 1 and page.evaluate('window.wins') == 1
    search(False); stroke(0); assert count() == 0
    page.locator('#word-search-check').click(); assert count() == 1
    page.locator('#word-search-check').click(); assert count() == 1
    page.locator('#reveal-solution').click(); assert count() == 1
    assert not errors, errors
    browser.close()
print('Progress sound browser checks passed for Crossword, Sudoku, Cross Equations, Word Pyramid and Word Search.')
