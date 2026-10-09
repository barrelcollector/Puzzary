"""Isolated API and two-browser QA for Group Solve host succession."""
from __future__ import annotations

import base64
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

database = Path(tempfile.gettempdir()) / "puzzary-group-host-transfer-qa.sqlite"
database.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-host-transfer-secret"
os.environ["UPLOAD_DIR"] = str(Path(tempfile.gettempdir()) / "puzzary-host-transfer-uploads")
os.environ["GROUP_HOST_RECONNECT_GRACE_SECONDS"] = "1"

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.groups import now
from app.main import SESSION_SECRET, hash_password
from app.models import GroupMember, GroupSolve, PermissionRole, Puzzle, User
from app.permissions import initialize_permissions
from app.puzzles import get_puzzle_type


Base.metadata.create_all(engine)
with SessionLocal() as db:
    initialize_permissions(db)
    player_role = db.scalar(select(PermissionRole).where(PermissionRole.name == "Player"))
    users = [
        User(
            display_name=name,
            is_admin=False,
            is_active=True,
            session_version=1,
            password_hash=hash_password("Host-transfer-test-123"),
            permissions_initialized=name == "QA participant",
            roles=[player_role] if name == "QA participant" else [],
        )
        for index, name in enumerate(["QA host", "QA first", "QA chosen", "QA participant"])
    ]
    db.add_all(users)
    db.flush()
    ids = [user.id for user in users]
    plugin = get_puzzle_type("takuzu")
    generated = plugin.manual_builder({"solution_grid": [[0, 0, 1, 1], [0, 1, 0, 1], [1, 0, 1, 0], [1, 1, 0, 0]], "given_count": 0})
    db.add(Puzzle(
        id="host-transfer-qa", puzzle_type="takuzu", title="Host transfer QA", theme="", difficulty="easy",
        prompt=generated.prompt, play_payload=generated.play_payload, print_payload=generated.print_payload,
        solution_payload=generated.solution_payload, puzzle_metadata={}, source="test",
    ))
    db.commit()


def cookie(user_id: int) -> str:
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_version": 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


def create_group(context) -> dict:
    response = context.request.post(base + "/api/puzzles/host-transfer-qa/groups", data={})
    assert response.ok, response.text()
    return response.json()


def join(context, code: str) -> dict:
    response = context.request.post(base + "/api/groups/join", data={"code": code})
    assert response.ok, response.text()
    return response.json()


def make_host_stale(group_id: str, age: float) -> None:
    with SessionLocal() as db:
        group = db.get(GroupSolve, group_id)
        member = db.scalar(select(GroupMember).where(
            GroupMember.group_id == group_id, GroupMember.user_id == group.host_id
        ))
        member.seen_at = now() - timedelta(seconds=age)
        db.commit()


def make_member_stale(group_id: str, user_id: int, age: float) -> None:
    with SessionLocal() as db:
        member = db.scalar(select(GroupMember).where(
            GroupMember.group_id == group_id, GroupMember.user_id == user_id
        ))
        member.seen_at = now() - timedelta(seconds=age)
        db.commit()


base = "http://127.0.0.1:8796"
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8796"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        contexts = []
        for user_id in ids:
            context = browser.new_context(viewport={"width": 1280, "height": 1000})
            context.add_cookies([{"name": "session", "value": cookie(user_id), "url": base}])
            contexts.append(context)
        host, first, chosen, participant = contexts
        for _ in range(100):
            try:
                if host.request.get(base + "/health").ok:
                    break
            except Exception:
                time.sleep(.1)
        else:
            raise RuntimeError("Host-transfer QA server failed to start")

        # Voluntary transfer supports an explicit target and updates both browsers immediately.
        group = create_group(host)
        endpoint = base + "/api/groups/" + group["id"]
        for context in (first, chosen, participant):
            join(context, group["code"])
        initial = host.request.get(endpoint).json()
        editable = next(
            f"{x},{y}" for y in range(4) for x in range(4)
            if f"{x},{y}" not in generated.solution_payload["givens"]
        )
        x, y = map(int, editable.split(","))
        edit_value = str(generated.solution_payload["grid"][y][x])
        action = host.request.post(endpoint + "/actions", data={
            "action_id": str(uuid.uuid4()), "revision": initial["revision"], "kind": "patch", "changes": {editable: edit_value},
        })
        assert action.ok, action.text()

        host_page = host.new_page()
        chosen_page = chosen.new_page()
        host_page.goto(base + group["url"])
        chosen_page.goto(base + group["url"])
        for page in (host_page, chosen_page):
            page.wait_for_function("!document.querySelector('.puzzle-layout > .panel').inert")
        assert host_page.get_by_role("button", name="Transfer host to QA chosen").is_visible()
        assert host_page.get_by_role("button", name="Transfer host to QA participant").count() == 0
        assert chosen_page.locator("#group-host").is_hidden()
        host_page.once("dialog", lambda dialog: dialog.accept())
        host_page.get_by_role("button", name="Transfer host to QA chosen").click()
        chosen_page.wait_for_function(f"document.querySelector('[data-player=\"{ids[2]}\"]')?.textContent.includes('(host)')")
        chosen_page.wait_for_function("!document.querySelector('#group-host').hidden")
        host_page.wait_for_function("document.querySelector('#group-host').hidden")
        assert "QA chosen is now the Group Solve host" in chosen_page.locator("#group-message").inner_text()
        assert chosen.request.get(endpoint).json()["state"]["answers"][editable] == edit_value
        assert host.request.post(endpoint + "/host", data={"user_id": ids[1]}).status == 403
        assert chosen.request.post(endpoint + "/host", data={"user_id": ids[3]}).status == 422
        chosen_page.once("dialog", lambda dialog: dialog.accept())
        chosen_page.get_by_role("button", name="Transfer host to QA host").click()
        host_page.wait_for_function("!document.querySelector('#group-host').hidden")
        assert host.request.get(endpoint).json()["host_id"] == ids[0]

        # Explicit departure promotes the earliest joined connected eligible member.
        departed = create_group(host)
        departed_endpoint = base + "/api/groups/" + departed["id"]
        join(first, departed["code"])
        join(chosen, departed["code"])
        settings = host.request.post(departed_endpoint + "/settings", data={"participant_limit": 7, "approval_required": False})
        assert settings.ok
        before = host.request.get(departed_endpoint).json()
        make_member_stale(departed["id"], ids[1], 25)
        leave = host.request.post(departed_endpoint + "/leave", data={})
        assert leave.ok and leave.json()["host_change"]["host_id"] == ids[1]
        after = first.request.get(departed_endpoint).json()
        assert after["host_id"] == ids[1] and after["participant_limit"] == 7
        assert after["state"] == before["state"] and after["recent"] == before["recent"]
        make_member_stale(departed["id"], ids[2], 25)
        assert first.request.post(departed_endpoint + "/host", data={"user_id": ids[2]}).status == 409
        assert first.request.get(departed_endpoint).json()["host_id"] == ids[1]
        join(host, departed["code"])
        assert host.request.get(departed_endpoint).json()["host_id"] == ids[1], "Former host reclaimed ownership"

        # A short disconnect stays within grace; a longer one promotes by join order.
        disconnected = create_group(host)
        disconnected_endpoint = base + "/api/groups/" + disconnected["id"]
        join(first, disconnected["code"])
        join(chosen, disconnected["code"])
        make_host_stale(disconnected["id"], .5)
        assert first.request.get(disconnected_endpoint).json()["host_id"] == ids[0]
        make_host_stale(disconnected["id"], 2)
        transferred = chosen.request.get(disconnected_endpoint)
        assert transferred.ok and transferred.json()["host_id"] == ids[1]
        assert transferred.json()["host_reconnect_grace_seconds"] == 1
        assert first.request.get(disconnected_endpoint).json()["host_id"] == ids[1]
        join(host, disconnected["code"])
        assert host.request.get(disconnected_endpoint).json()["host_id"] == ids[1]

        # With no eligible signed-in successor, the session closes; guests cannot become host.
        empty = create_group(host)
        empty_endpoint = base + "/api/groups/" + empty["id"]
        with SessionLocal() as db:
            db.add(GroupMember(group_id=empty["id"], guest_name="QA guest", guest_token_hash="not-a-real-token", status="active", seen_at=now()))
            db.commit()
        closed = host.request.post(empty_endpoint + "/leave", data={})
        assert closed.ok and closed.json()["host_change"]["closed"]
        with SessionLocal() as db:
            saved = db.get(GroupSolve, empty["id"])
            assert saved.closed and saved.host_id == ids[0]

        chosen_page.set_viewport_size({"width": 390, "height": 844})
        chosen_page.screenshot(path=str(Path(tempfile.gettempdir()) / "group-host-transfer-mobile.png"), full_page=True)
        assert chosen_page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        browser.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
    engine.dispose()
    try:
        database.unlink(missing_ok=True)
    except PermissionError:
        pass

print("Group host voluntary handoff, authorization, capability checks, realtime UI, deterministic leave/disconnect succession, grace, state preservation, former-host rejoin, guest exclusion, closure and mobile layout passed.")
