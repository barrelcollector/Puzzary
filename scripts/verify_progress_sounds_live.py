"""Read-only live smoke check for shared sound wiring across puzzle pages."""
import base64
import json

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import select

from app.database import SessionLocal
from app.main import SESSION_SECRET
from app.models import Puzzle, User

base = 'http://127.0.0.1:8790'
types = ['word_search','crossword','word_pyramid','sudoku','cross_equations','nonogram','maze']
with SessionLocal() as db:
    user = db.scalar(select(User).where(User.is_admin == True, User.is_active == True).order_by(User.id))
    payload = base64.b64encode(json.dumps({'user_id':user.id,'session_version':user.session_version or 1}).encode())
    cookie = TimestampSigner(SESSION_SECRET).sign(payload).decode()
    puzzles = [(kind, db.scalar(select(Puzzle.id).where(Puzzle.puzzle_type == kind).order_by(Puzzle.created_at.desc()))) for kind in types]

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context()
    context.add_cookies([{'name':'session','value':cookie,'url':base}])
    errors = []
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    for kind, puzzle_id in puzzles:
        if not puzzle_id:
            print('No live sample for',kind)
            continue
        response = page.goto(base+'/puzzles/'+puzzle_id)
        assert response.ok, (kind, response.status)
        assert page.evaluate('typeof window.PuzzarySounds.progress === "function" && typeof window.PuzzarySounds.createProgressTracker === "function"')
        if kind == 'cross_equations':
            assert page.locator('.cross-equations-play').get_attribute('data-equations')
        if kind == 'sudoku':
            assert page.locator('.sudoku-play').get_attribute('data-box-rows')
        print('Live page passed:',kind)
    assert not errors, errors
    browser.close()
