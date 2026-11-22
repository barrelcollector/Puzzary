"""Browser coverage for secure offline downloads, persistence, sync, and conflicts."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright

from qa_runtime import qa_directory


ROOT = Path(__file__).resolve().parents[1]
QA_ROOT = qa_directory("offline-puzzles")
DATABASE = QA_ROOT / "offline.sqlite"
DATABASE.unlink(missing_ok=True)
PLUGIN_ROOT = QA_ROOT / "plugins"
PACKAGE = PLUGIN_ROOT / "offline_test"
(PACKAGE / "templates" / "puzzle_types").mkdir(parents=True, exist_ok=True)
(PACKAGE / "static").mkdir(parents=True, exist_ok=True)
(PACKAGE / "puzzary-plugin.json").write_text(json.dumps({
    "schema": "puzzary.plugin.v1", "api_version": "1", "slug": "offline_test",
    "name": "Offline Test", "version": "1.0.0", "entrypoint": "plugin.py",
    "templates": "templates", "static": "static",
}), encoding="utf-8")
(PACKAGE / "plugin.py").write_text("""
from app.puzzles import GeneratedPuzzle, PuzzleType, register_puzzle_type
def normalize(payload, solution):
    answers = getattr(payload, 'answers', {}) if payload is not None else {}
    return {'answers': {str(k): str(v) for k, v in answers.items()} if isinstance(answers, dict) else {}}
def count(progress, solution):
    return (1 if (progress or {}).get('answers', {}).get('value') == solution.get('answer') else 0, 1)
def offline_solution(solution):
    return {'answers': {'value': str(solution.get('answer', ''))}}
def offline_parts(solution):
    return {'entry-1': offline_solution(solution)}
def offline_game_data(solution):
    return {'public_board': 'downloaded-board'}
def generate(*args, **kwargs):
    return GeneratedPuzzle('Offline test', 'Enter a value.', {}, {}, {'answer':'correct'}, [], {})
register_puzzle_type(PuzzleType(slug='offline_test', label='Offline Test', generator=generate,
    source_builder=lambda *_: {}, answer_checker=lambda answer, solution: (False,1,0),
    progress_normalizer=normalize, progress_counter=count, supports_print=False,
    play_script='/plugins/offline_test/static/play.js?v=1', supports_offline=True,
    offline_progress_fields=('answers',), offline_validation='solution_hash',
    offline_solution_state=offline_solution, offline_solution_parts=offline_parts,
    offline_game_data=offline_game_data, offline_notes='Progress and checks work offline.'))
""", encoding="utf-8")
(PACKAGE / "templates" / "puzzle_types" / "offline_test.html").write_text("""
<div class="offline-test" data-answers='{{ progress_answers | tojson }}' data-progress-url="/api/puzzles/{{ puzzle.id }}/progress">
  <label>Value <input data-offline-value autocomplete="off"></label>
  <button type="button" data-offline-check>Check</button>
  <p class="muted" role="status" data-test-status>Ready.</p>
</div>
""", encoding="utf-8")
(PACKAGE / "static" / "play.js").write_text("""
(() => { const root=document.querySelector('.offline-test'); if(!root)return;
const input=root.querySelector('input'),status=root.querySelector('[data-test-status]');
try{input.value=JSON.parse(root.dataset.answers||'{}').value||'';}catch(_e){}
window.PuzzarySolo?.register({read:()=>({value:input.value}),apply:state=>{input.value=state.answers?.value||'';}});
root.querySelector('[data-offline-check]').addEventListener('click',()=>window.PuzzarySolo?.action('check'));
input.addEventListener('change',async()=>{const r=await fetch(root.dataset.progressUrl,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answers:{value:input.value}})});status.textContent=r.ok?'Saved.':'Save failed.';});root.dataset.ready='true'; })();
""", encoding="utf-8")

BASE = "http://127.0.0.1:8823"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{DATABASE.as_posix()}",
    "SESSION_SECRET": "isolated-offline-test-secret",
    "UPLOAD_DIR": str(QA_ROOT / "uploads"),
    "APP_BASE_URL": BASE,
    "PUZZARY_PLUGIN_DIR": str(PLUGIN_ROOT),
})

from app.database import Base, SessionLocal, engine
from app.main import SESSION_SECRET, hash_password
from app.models import Puzzle, PuzzleProgress, User
from app.permissions import initialize_permissions


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
with SessionLocal() as db:
    admin = User(display_name="Offline Admin", is_admin=True, is_active=True, session_version=1,
                 password_hash=hash_password("Offline-test-123"))
    player = User(display_name="Offline Player", is_admin=False, is_active=True, session_version=1,
                  password_hash=hash_password("Offline-test-123"))
    db.add_all([admin, player])
    db.commit()
    initialize_permissions(db)
    db.refresh(admin)
    db.refresh(player)
    admin_id, user_id = admin.id, player.id
    common = dict(puzzle_type="offline_test", theme="qa", difficulty="easy", prompt="Public prompt",
                  print_payload={}, play_payload={"kind": "offline_test"},
                  solution_payload={"answer": "correct", "secret": "NEVER_CACHE_THIS_SOLUTION"},
                  source="test", created_by_id=admin_id)
    db.add_all([
        Puzzle(id="offline-public", title="Offline Public", puzzle_metadata={}, **common),
        Puzzle(id="offline-protected", title="Offline Protected", puzzle_metadata={}, access_password_hash=hash_password("Puzzle-test-123"), **common),
        Puzzle(id="offline-hidden", title="Offline Hidden", puzzle_metadata={"visibility": "unlisted"}, **common),
        Puzzle(id="offline-disabled", title="Offline Disabled", puzzle_metadata={"offline_disabled": True}, **common),
        Puzzle(id="offline-scheduled", title="Offline Scheduled", puzzle_metadata={}, publish_at=datetime.now(timezone.utc) + timedelta(days=1), **common),
    ])
    db.commit()


def session_cookie(user_id: int) -> str:
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_version": 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8823"],
    cwd=ROOT, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        desktop = browser.new_context(service_workers="allow", viewport={"width": 1280, "height": 900})
        desktop.add_cookies([{"name": "session", "value": session_cookie(user_id), "url": BASE}])
        admin_context = browser.new_context()
        admin_context.add_cookies([{"name": "session", "value": session_cookie(admin_id), "url": BASE}])
        page = desktop.new_page()
        for _ in range(120):
            try:
                if desktop.request.get(BASE + "/health").ok:
                    break
            except Exception:
                time.sleep(.1)
        else:
            raise RuntimeError("Offline QA server did not start")

        page.goto(BASE + "/puzzles/offline-public")
        page.wait_for_function("navigator.serviceWorker && navigator.serviceWorker.ready")
        page.reload()
        page.wait_for_function("Boolean(navigator.serviceWorker.controller)")
        assert page.locator("[data-offline-download]").count() == 1
        page.locator("[data-offline-download]").click()
        page.wait_for_function("document.querySelector('[data-offline-download]').dataset.downloaded === 'true'")

        cached_text = page.evaluate("""async () => { let text=''; for (const name of await caches.keys()) { const cache=await caches.open(name); for (const request of await cache.keys()) text += await (await cache.match(request)).clone().text(); } return text; }""")
        assert "NEVER_CACHE_THIS_SOLUTION" not in cached_text
        assert "Admin Solution" not in cached_text
        assert "downloaded-board" in cached_text

        assert desktop.request.get(BASE + "/api/puzzles/offline-protected/offline-package").status == 423
        assert desktop.request.get(BASE + "/api/puzzles/offline-hidden/offline-package").status == 409
        disabled = desktop.request.get(BASE + "/api/puzzles/offline-disabled/offline-package")
        assert disabled.status == 409 and "disabled" in disabled.text().lower() and "digest" not in disabled.text().lower() and "game_data" not in disabled.text().lower()
        enabled = admin_context.request.post(BASE + "/admin/puzzles/offline-disabled/metadata", form={
            "category_id": 0, "completion_message": "", "visibility": "public", "offline_allowed": "true",
        }, max_redirects=0)
        assert enabled.status == 303 and desktop.request.get(BASE + "/api/puzzles/offline-disabled/offline-package").ok
        disabled_again = admin_context.request.post(BASE + "/admin/puzzles/offline-disabled/metadata", form={
            "category_id": 0, "completion_message": "", "visibility": "public", "offline_allowed": "false",
        }, max_redirects=0)
        assert disabled_again.status == 303
        blocked_again = desktop.request.get(BASE + "/api/puzzles/offline-disabled/offline-package")
        assert blocked_again.status == 409 and "digest" not in blocked_again.text().lower()
        assert desktop.request.get(BASE + "/api/puzzles/offline-scheduled/offline-package").status == 403

        desktop.set_offline(True)
        page.goto(BASE + "/puzzles/offline-public", wait_until="domcontentloaded")
        assert "Offline copy" in page.locator("body").inner_text()
        page.wait_for_function("document.querySelector('.offline-test')?.dataset.ready === 'true'")
        page.locator("[data-offline-value]").fill("wrong")
        page.locator("[data-offline-check]").click()
        page.wait_for_function("document.querySelector('[data-test-status]').textContent.includes('not complete')")
        page.locator("[data-offline-value]").fill("correct")
        page.locator("[data-offline-check]").click()
        page.wait_for_function("document.querySelector('[data-test-status]').textContent.includes('checks out offline')")
        part_result = page.evaluate("PuzzaryOfflineCheck({answers:{value:'correct'}}, 'entry-1')")
        assert part_result["available"] and part_result["correct"]
        page.locator("[data-offline-value]").fill("device-value")
        page.locator("[data-offline-value]").dispatch_event("change")
        page.wait_for_function("document.querySelector('[data-test-status]').textContent === 'Saved.'")
        page.reload(wait_until="domcontentloaded")
        assert page.locator("[data-offline-value]").input_value() == "device-value"

        desktop.set_offline(False)
        page.wait_for_function("navigator.onLine")
        for _ in range(50):
            with SessionLocal() as db:
                progress = db.query(PuzzleProgress).filter_by(puzzle_id="offline-public", user_id=user_id).one_or_none()
                if progress and progress.progress_payload.get("answers", {}).get("value") == "device-value":
                    break
            time.sleep(.1)
        else:
            raise AssertionError("Offline progress did not synchronize")

        desktop.set_offline(True)
        page.locator("[data-offline-value]").fill("offline-conflict")
        page.locator("[data-offline-value]").dispatch_event("change")
        page.wait_for_function("document.querySelector('[data-test-status]').textContent === 'Saved.'")
        with SessionLocal() as db:
            progress = db.query(PuzzleProgress).filter_by(puzzle_id="offline-public", user_id=user_id).one()
            progress.progress_payload = {"answers": {"value": "other-device"}}
            progress.updated_at = datetime.now(timezone.utc) + timedelta(seconds=1)
            db.commit()
        desktop.set_offline(False)
        page.wait_for_timeout(500)
        page.goto(BASE + "/offline")
        assert page.locator("[data-offline-list]").count() == 1, page.locator("body").inner_text()
        page.locator("[data-offline-sync]").click()
        page.wait_for_function("document.querySelector('[data-offline-list]')?.textContent.includes('Progress changed')")
        page.locator('[data-conflict="server"]').click()
        page.wait_for_function("!document.querySelector('[data-conflict]')")

        desktop.set_offline(True)
        page.goto(BASE + "/puzzles/offline-public", wait_until="domcontentloaded")
        assert page.locator("[data-offline-value]").input_value() == "other-device"
        desktop.set_offline(False)

        mobile = browser.new_context(service_workers="allow", viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        mobile.add_cookies([{"name": "session", "value": session_cookie(user_id), "url": BASE}])
        mobile_page = mobile.new_page()
        mobile_page.goto(BASE + "/offline")
        assert mobile_page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        mobile.close()
        browser.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()

print("Offline download security, persistence, synchronization, conflicts, and responsive layouts passed.")
