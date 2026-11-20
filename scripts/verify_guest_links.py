"""Isolated lifecycle and security coverage for expiring puzzle guest links."""
import os
from qa_runtime import qa_directory

qa_root = qa_directory("guest-links")
os.environ["DATABASE_URL"] = f"sqlite:///{(qa_root / 'guest-links.sqlite').as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-guest-links-secret"
os.environ["UPLOAD_DIR"] = str(qa_root / "uploads")
os.environ["APP_BASE_URL"] = "http://127.0.0.1:8798"

import base64
from datetime import datetime, timedelta, timezone
import html
import json
import re
import subprocess
import sys
import time

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import inspect, select

from app.database import Base, SessionLocal, engine
from app.main import SESSION_SECRET, hash_password
from app.models import GroupMember, GroupSolve, PermissionAudit, Puzzle, PuzzleGuestLink, PuzzleGuestSession, User
from app.puzzles import get_puzzle_type


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
tables = set(inspect(engine).get_table_names())
assert {"puzzle_guest_links", "puzzle_guest_sessions"} <= tables
with SessionLocal() as db:
    admin = User(display_name="Guest Link Admin", is_admin=True, is_active=True, session_version=1,
                 password_hash=hash_password("Account-test-123"))
    db.add(admin)
    db.flush()
    admin_id = admin.id
    generated = get_puzzle_type("takuzu").manual_builder({
        "solution_grid": [[0, 0, 1, 1], [0, 1, 0, 1], [1, 0, 1, 0], [1, 1, 0, 0]],
        "given_count": 0, "seed": "guest-link-qa",
    })
    for puzzle_id, title, metadata, archived in [
        ("guest-link-puzzle", "Private Guest Quasar", {"visibility": "unlisted"}, None),
        ("guest-link-draft", "Draft Guest Puzzle", {"draft": True}, None),
        ("guest-link-archived", "Archived Guest Puzzle", {}, datetime.now(timezone.utc)),
    ]:
        db.add(Puzzle(
            id=puzzle_id, puzzle_type="takuzu", title=title, theme="guest-link-qa", difficulty="easy",
            prompt="Secret guest prompt", play_payload=generated.play_payload, print_payload=generated.print_payload,
            solution_payload=generated.solution_payload, puzzle_metadata=metadata, source="test",
            created_by_id=admin_id, archived_at=archived,
            access_password_hash=hash_password("Puzzle-Guest-123") if puzzle_id == "guest-link-puzzle" else None,
        ))
    db.commit()


def session_cookie(user_id):
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_version": 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


def created_url(response):
    assert response.status == 201, (response.status, response.text())
    match = re.search(r'<input[^>]*value="([^"]+)"[^>]*data-guest-link-url', response.text())
    assert match, response.text()[:1000]
    return html.unescape(match.group(1))


base = "http://127.0.0.1:8798"
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8798"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        admin = browser.new_context()
        guest = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        second = browser.new_context()
        admin.add_cookies([{"name": "session", "value": session_cookie(admin_id), "url": base}])
        for _ in range(100):
            try:
                if guest.request.get(base + "/health").ok:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("QA server did not start")

        create = admin.request.post(base + "/admin/puzzles/guest-link-puzzle/guest-links", form={
            "label": "Family weekend", "max_uses": 2, "max_concurrent_guests": 2,
            "allow_group_solve": "true",
        })
        share_url = created_url(create)
        token = share_url.rsplit("/", 1)[-1]
        assert token not in admin.request.get(base + "/admin/puzzles").text()
        with SessionLocal() as db:
            link = db.scalar(select(PuzzleGuestLink).where(PuzzleGuestLink.puzzle_id == "guest-link-puzzle"))
            assert link and link.token_hash and token not in link.token_hash
            assert link.max_uses == 2 and link.max_concurrent_guests == 2 and link.allow_group_solve
            assert db.scalar(select(PermissionAudit).where(PermissionAudit.subject_id == link.id, PermissionAudit.action == "guest_link.created"))
            link_id = link.id

        join = guest.new_page()
        response = join.goto(share_url)
        assert response.ok and "Private Guest Quasar" in join.locator("body").inner_text()
        assert "Secret guest prompt" not in join.locator("body").inner_text()
        assert join.locator("input[name=puzzle_password]").count() == 1
        assert join.evaluate("document.documentElement.scrollWidth <= innerWidth")
        join.locator("input[name=display_name]").fill("Weekend Guest")
        join.locator("input[name=puzzle_password]").fill("wrong")
        join.locator("button[type=submit]").click()
        assert "not accepted" in join.locator("[role=alert]").inner_text()
        join.locator("input[name=display_name]").fill("Weekend Guest")
        join.locator("input[name=puzzle_password]").fill("Puzzle-Guest-123")
        join.locator("button[type=submit]").click()
        join.wait_for_url("**/puzzles/guest-link-puzzle")
        assert "Private Guest Quasar" in join.locator("body").inner_text()
        assert "Weekend Guest (guest)" in join.locator("body").inner_text()
        assert join.locator("form[action$='/photos']").count() == 0

        saved = guest.request.post(base + "/api/puzzles/guest-link-puzzle/progress", data={"answers": {"0,0": "1"}})
        assert saved.ok, saved.text()
        restored = guest.request.get(base + "/api/puzzles/guest-link-puzzle/progress").json()
        assert restored["answers"]["0,0"] == "1"
        assert guest.request.get(base + "/api/puzzles").status == 401
        assert guest.request.get(base + "/api/puzzles/guest-link-puzzle/print-preview").status == 403
        assert guest.request.post(base + "/api/puzzles/guest-link-puzzle/photos", multipart={}).status in (400, 403, 422)
        with SessionLocal() as db:
            session = db.scalar(select(PuzzleGuestSession).where(PuzzleGuestSession.link_id == link_id))
            assert session and session.progress_payload["answers"]["0,0"] == "1"
            assert session.display_name == "Weekend Guest"
            db.add(GroupSolve(
                id="guest-link-group", code="GUESTLINK", puzzle_id="guest-link-puzzle",
                host_id=admin_id, participant_limit=4, allow_guests=True,
                state={"answers": {}}, cell_versions={}, puzzle_states={},
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            ))
            db.add(GroupMember(group_id="guest-link-group", user_id=admin_id, status="active"))
            db.commit()
        group_join = guest.request.post(base + "/api/groups/join", data={"code": "GUESTLINK"})
        assert group_join.ok, group_join.text()
        with SessionLocal() as db:
            group_guest = db.scalar(select(GroupMember).where(
                GroupMember.group_id == "guest-link-group", GroupMember.user_id.is_(None)))
            assert group_guest and group_guest.guest_name == "Weekend Guest" and group_guest.status == "active"

        second_join = second.request.post(share_url, form={
            "display_name": "Second Guest", "puzzle_password": "Puzzle-Guest-123",
        }, max_redirects=0)
        assert second_join.status == 303
        third = browser.new_context()
        use_limited = third.request.post(share_url, form={
            "display_name": "Third Guest", "puzzle_password": "Puzzle-Guest-123",
        })
        assert use_limited.status == 410 and "use limit" in use_limited.text().lower()

        concurrent_create = admin.request.post(base + "/admin/puzzles/guest-link-puzzle/guest-links", form={
            "label": "One at a time", "max_uses": 0, "max_concurrent_guests": 1,
        })
        concurrent_url = created_url(concurrent_create)
        one = browser.new_context(); two = browser.new_context()
        assert one.request.post(concurrent_url, form={"display_name": "One", "puzzle_password": "Puzzle-Guest-123"}, max_redirects=0).status == 303
        blocked = two.request.post(concurrent_url, form={"display_name": "Two", "puzzle_password": "Puzzle-Guest-123"})
        assert blocked.status == 409 and "slots" in blocked.text().lower()
        with SessionLocal() as db:
            c_link = db.scalar(select(PuzzleGuestLink).where(PuzzleGuestLink.label == "One at a time"))
            c_session = db.scalar(select(PuzzleGuestSession).where(PuzzleGuestSession.link_id == c_link.id))
            c_session.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=21)
            db.commit()
        assert two.request.post(concurrent_url, form={"display_name": "Two", "puzzle_password": "Puzzle-Guest-123"}, max_redirects=0).status == 303

        expired_token = "expired-guest-token"
        with SessionLocal() as db:
            db.add(PuzzleGuestLink(
                id="expired-guest-link", puzzle_id="guest-link-puzzle",
                token_hash=__import__("hashlib").sha256(expired_token.encode()).hexdigest(),
                label="Expired", created_by_id=admin_id,
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            ))
            db.commit()
        assert guest.request.get(base + "/share/" + expired_token).status == 410
        assert admin.request.post(base + "/admin/puzzles/guest-link-draft/guest-links", form={}).status == 409
        assert admin.request.post(base + "/admin/puzzles/guest-link-archived/guest-links", form={}).status == 409

        revoked = admin.request.post(
            base + f"/admin/puzzles/guest-link-puzzle/guest-links/{link_id}/revoke", max_redirects=0,
        )
        assert revoked.status == 303
        assert guest.request.get(base + "/api/puzzles/guest-link-puzzle/progress").status == 401
        assert guest.request.get(share_url).status == 410
        with SessionLocal() as db:
            link = db.get(PuzzleGuestLink, link_id)
            assert link.revoked_at is not None
            assert db.scalar(select(PermissionAudit).where(PermissionAudit.subject_id == link.id, PermissionAudit.action == "guest_link.revoked"))

        browser.close()
        print("Guest-link one-time creation/copy, hashed storage, password composition, persistent play-only progress, use/concurrency limits, expiry, revocation and auditing passed.")
finally:
    server.terminate()
    server.wait(timeout=15)
