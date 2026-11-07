"""Isolated Puzzary API and bundled Home Assistant integration checks."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import yaml


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
from app.models import PermissionRole, User
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
        restored = admin.post(BASE + f"/api/puzzles/{puzzle_id}/unarchive")
        assert restored.ok and restored.json()["publication_state"] == "published", restored.text()
        republished = admin.post(BASE + f"/api/puzzles/{puzzle_id}/publish")
        assert republished.ok and republished.json()["publication_state"] == "published"

        for context in (anonymous, admin, player):
            context.dispose()

    component = ROOT / "custom_components" / "puzzary"
    manifest = json.loads((component / "manifest.json").read_text(encoding="utf-8"))
    strings = json.loads((component / "strings.json").read_text(encoding="utf-8"))
    translations = json.loads((component / "translations" / "en.json").read_text(encoding="utf-8"))
    services = yaml.safe_load((component / "services.yaml").read_text(encoding="utf-8"))
    assert manifest["domain"] == "puzzary" and manifest["config_flow"] is True
    for filename in ("__init__.py", "api.py", "config_flow.py", "coordinator.py", "sensor.py", "image.py", "services.yaml"):
        assert (component / filename).is_file(), filename
    for service in ("refresh", "generate_puzzle", "publish_puzzle", "archive_puzzle", "unarchive_puzzle"):
        assert service in strings["services"]
        assert service in translations["services"] and service in services
        assert set(services[service]["fields"]) == set(translations["services"][service]["fields"])
    print("Home Assistant stats, latest selection, PNG rendering, permissions, admin actions, and bundle structure passed.")
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
    shutil.rmtree(QA_ROOT, ignore_errors=True)
