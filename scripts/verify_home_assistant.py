"""Isolated Puzzary API checks used by the Home Assistant integration."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QA_ROOT = Path(tempfile.gettempdir()) / "puzzary-home-assistant-qa"
QA_DB = QA_ROOT / "home-assistant.sqlite"
if QA_ROOT.exists():
    shutil.rmtree(QA_ROOT)
QA_ROOT.mkdir(parents=True)

os.environ["DATABASE_URL"] = f"sqlite:///{QA_DB.as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-home-assistant-secret"
os.environ["UPLOAD_DIR"] = str(QA_ROOT / "uploads")
os.environ["APP_BASE_URL"] = "https://puzzary.example.test"

from playwright.sync_api import sync_playwright
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.main import hash_secret
from app.models import PermissionRole, Puzzle, User
from app.permissions import initialize_permissions


ADMIN_TOKEN = "home-assistant-admin-token"
PLAYER_TOKEN = "home-assistant-player-token"
Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
with SessionLocal() as db:
    admin = User(
        display_name="Home Assistant Admin",
        is_admin=True,
        is_active=True,
        api_token_hash=hash_secret(ADMIN_TOKEN),
        api_token_prefix="homeassistant",
    )
    db.add(admin)
    db.commit()
    initialize_permissions(db)
    player_role = db.scalar(select(PermissionRole).where(PermissionRole.name == "Player"))
    player = User(
        display_name="Home Assistant Player",
        is_active=True,
        roles=[player_role],
        permissions_initialized=True,
        api_token_hash=hash_secret(PLAYER_TOKEN),
        api_token_prefix="homeassistant",
    )
    db.add(player)
    db.commit()


BASE = "http://127.0.0.1:8812"
server = subprocess.Popen(
    ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8812"],
    cwd=ROOT,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        anonymous = playwright.request.new_context()
        admin = playwright.request.new_context(extra_http_headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        player = playwright.request.new_context(extra_http_headers={"Authorization": f"Bearer {PLAYER_TOKEN}"})
        for _ in range(100):
            try:
                if anonymous.get(BASE + "/health").ok:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("Home Assistant API QA server did not start")

        assert anonymous.get(BASE + "/api/account/stats").status == 401
        stats = player.get(BASE + "/api/account/stats")
        assert stats.ok and stats.json()["totals"]["played"] == 0

        generated = admin.post(BASE + "/api/puzzles/generate", data={
            "puzzle_type": "takuzu",
            "difficulty": "easy",
            "seed": "home-assistant-qa",
            "generation_options": {"grid_size": 4, "given_count": 6},
        })
        assert generated.ok, generated.text()
        puzzle = generated.json()
        puzzle_id = puzzle["id"]

        latest = player.get(BASE + "/api/puzzles/latest?printable=true")
        assert latest.ok and latest.json()["id"] == puzzle_id
        protected = admin.post(BASE + "/api/puzzles/generate", data={
            "puzzle_type": "takuzu",
            "difficulty": "easy",
            "seed": "home-assistant-protected-media-qa",
            "generation_options": {"grid_size": 4, "given_count": 6},
            "access_password": "Media-Test-Password1!",
        })
        assert protected.ok and protected.json()["password_protected"] is True, protected.text()
        with SessionLocal() as db:
            db.add(Puzzle(
                id="digital-only-media-fixture",
                puzzle_type="unknown_digital",
                title="Digital-only fixture",
                theme="test",
                difficulty="easy",
                prompt="",
                print_payload={},
                play_payload={},
                solution_payload={},
                puzzle_metadata={},
                source="test",
            ))
            db.commit()
        media_list = player.get(BASE + "/api/puzzles?availability=active&printable=true&limit=200")
        assert media_list.ok, media_list.text()
        assert [item["id"] for item in media_list.json()["items"]] == [puzzle_id]
        assert media_list.json()["filters"]["printable"] is True
        image = player.get(BASE + f"/api/puzzles/{puzzle_id}/print.png")
        assert image.ok, image.text()
        assert image.headers["content-type"].startswith("image/png")
        assert image.body()[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(image.body()) > 1_000
        pdf = player.get(BASE + f"/api/puzzles/{puzzle_id}/print.pdf")
        assert pdf.ok and pdf.body().startswith(b"%PDF")

        denied_archive = player.post(BASE + f"/api/puzzles/{puzzle_id}/archive")
        assert denied_archive.status == 403, f"{denied_archive.status}: {denied_archive.text()}"
        archived = admin.post(BASE + f"/api/puzzles/{puzzle_id}/archive")
        assert archived.ok and archived.json()["publication_state"] == "archived"
        archived_media = admin.get(BASE + "/api/puzzles?availability=archived&printable=true&limit=200")
        assert archived_media.ok and archived_media.json()["items"] and archived_media.json()["items"][0]["id"] == puzzle_id, archived_media.text()
        assert not player.get(BASE + "/api/puzzles?availability=archived&printable=true&limit=200").json()["items"]
        restored = admin.post(BASE + f"/api/puzzles/{puzzle_id}/unarchive")
        assert restored.ok and restored.json()["publication_state"] == "published", restored.text()
        republished = admin.post(BASE + f"/api/puzzles/{puzzle_id}/publish")
        assert republished.ok and republished.json()["publication_state"] == "published"

        for context in (anonymous, admin, player):
            context.dispose()

    print("Home Assistant stats, media folders, printable filtering, PNG/PDF rendering, permissions, and admin API actions passed.")
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
    shutil.rmtree(QA_ROOT, ignore_errors=True)
