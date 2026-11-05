"""Offline browser checks for Minesweeper controls, rendering, and mobile fit."""
import json
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import sync_playwright
from starlette.templating import Jinja2Templates

from app.puzzle_types.minesweeper import counts_for


root = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(str(root / "app" / "templates"))
static = root / "app" / "static"
puzzle = SimpleNamespace(id="mine-test", prompt="Clear the board.")
html = templates.env.get_template("puzzle_types/minesweeper.html").render(
    puzzle=puzzle,
    play_payload={"width": 5, "height": 5, "mine_count": 3},
    progress_payload={"answers": {}, "status": "playing", "exploded": ""},
)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 390, "height": 844})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_content(f"<style>{(static / 'styles.css').read_text(encoding='utf-8')}</style>" + html + '<button id="reveal-solution">Reveal solution</button>')
    page.evaluate("""
      window.actions=[]; window.wins=0; window.resets=0;
      window.PuzzaryCelebrate={complete:()=>window.wins++,reset:()=>window.resets++};
      window.PuzzarySolo={
        register(adapter){window.adapter=adapter},
        async action(kind,target=''){
          window.actions.push([kind,target]);
          const base={answers:{...(window.state?.answers||{})},status:'playing',exploded:''};
          if(kind==='flag') base.answers[target]=base.answers[target]==='F'?undefined:'F';
          if(kind==='reveal_cell') base.answers[target]='1';
          if(kind==='hint') base.answers['4,4']='0';
          if(kind==='reset') base.answers={};
          if(kind==='reveal') Object.assign(base,{answers:{'0,0':'M','1,0':'1'},status:'revealed'});
          window.state=base; window.adapter.apply(base); return true;
        }
      };
    """)
    page.add_script_tag(content=(static / "minesweeper.js").read_text(encoding="utf-8"))
    cells = page.locator(".minesweeper-cell")
    assert cells.count() == 25
    cells.nth(0).click()
    assert page.evaluate("actions.at(-1)") == ["reveal_cell", "0,0"]
    assert cells.nth(0).inner_text() == "1"
    cells.nth(1).click(button="right")
    assert page.evaluate("actions.at(-1)") == ["flag", "1,0"]
    assert cells.nth(1).inner_text() == "⚑"
    cells.nth(2).focus(); cells.nth(2).press("f")
    assert page.evaluate("actions.at(-1)") == ["flag", "2,0"]
    cells.nth(2).press("ArrowDown")
    assert page.evaluate("document.activeElement.dataset.key") == "2,1"
    page.locator('[data-tool="flag"]').click(); cells.nth(3).click()
    assert page.evaluate("actions.at(-1)") == ["flag", "3,0"]
    page.locator(".minesweeper-hint").click()
    assert page.evaluate("actions.at(-1)[0]") == "hint"
    page.evaluate("adapter.apply({answers:{'0,0':'0'},status:'won',exploded:''})")
    assert page.evaluate("wins") == 1
    assert "won" in page.locator(".minesweeper-status").inner_text().lower()
    page.locator(".minesweeper-restart").click(force=True)
    assert page.evaluate("actions.at(-1)[0]") == "reset"
    page.locator("#reveal-solution").click()
    assert page.evaluate("actions.at(-1)[0]") == "reveal"
    assert page.locator(".minesweeper-cell.is-mine").count() == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    solution = {"width": 5, "height": 5, "mine_count": 3, "mines": ["0,0", "2,2", "4,4"]}
    solution["counts"] = counts_for(5, 5, set(solution["mines"]))
    solution_html = templates.env.get_template("puzzle_solutions/minesweeper.html").render(
        puzzle=SimpleNamespace(solution_payload=solution),
    )
    page.set_content(f"<style>{(static / 'styles.css').read_text(encoding='utf-8')}</style>" + solution_html)
    assert page.locator(".minesweeper-cell.is-mine").count() == 3
    assert "3 mines" in page.locator("body").inner_text()
    assert not errors, errors
    browser.close()

print("Minesweeper click, flag, context menu, keyboard, hint, reset, reveal, completion and mobile checks passed.")
