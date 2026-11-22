import re
import urllib.parse
from pathlib import Path


root = Path(__file__).resolve().parents[1]
readme = (root / "README.md").read_text(encoding="utf-8")
deployment = (root / "docs/docker-deployment.md").read_text(encoding="utf-8")
modules = (root / "docs/puzzle-modules.md").read_text(encoding="utf-8")
pwa = (root / "docs/progressive-web-app.md").read_text(encoding="utf-8")
env_example = (root / ".env.example").read_text(encoding="utf-8")
license_text = (root / "LICENSE").read_text(encoding="utf-8")
notice = (root / "NOTICE").read_text(encoding="utf-8")
docs = "\n".join(
    path.read_text(encoding="utf-8")
    for path in [root / "README.md", *sorted((root / "docs").glob("*.md"))]
)

assert "docs/docker-deployment.md" in readme
assert "docs/README.md" in readme
assert "docs/puzzle-modules.md" in readme
assert "docs/progressive-web-app.md" in readme
assert "https://github.com/barrelcollector/puzzary-official-plugins" in readme
assert len(readme.splitlines()) <= 160
for stale_heading in ("## Dictionary Content Pack", "## Nonogram / Picross", "## Takuzu / Binairo"):
    assert stale_heading not in readme, stale_heading
for marker in (
    "docker compose up -d --build",
    "./data/postgres",
    "versioned Alembic migrations",
    "alembic current",
    "v0.1.0-beta.1",
    "pg_dump",
    "pg_restore --list",
    "pg_restore --clean --if-exists",
    "puzzary-files.tar.gz",
    "curl --fail http://localhost:8790/health",
):
    assert marker in deployment, marker

for marker in (
    "register_puzzle_type",
    "GeneratedPuzzle",
    "play_payload",
    "print_payload",
    "solution_payload",
    "scripts/verify_<slug>.py",
    "verify_all_group_puzzles.py",
):
    assert marker in modules, marker

for marker in ("/service-worker.js", "secure browser context", "Authenticated HTML", "versioned shell cache"):
    assert marker in pwa, marker

assert "replace every change-* value" in env_example
assert "Apache License" in license_text and "Version 2.0" in license_text
for policy in ("NOTICE", "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md"):
    assert (root / policy).is_file(), policy
assert "Puzzary contributors" in notice
assert "app/static/icons/LICENSE" in notice
assert "distributed with\nthose packages" in notice
dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
assert "COPY LICENSE NOTICE /licenses/puzzary/" in dockerfile
assert not re.search(r"\b(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)", docs)
assert ("home " + "assistant") not in docs.lower()
assert ("puzzary-home-" + "assistant") not in docs.lower()
for stale_claim in (
    "all ten bundled games",
    "ten bundled fallback games",
    "repository is still private",
):
    assert stale_claim not in docs.lower(), stale_claim

markdown_files = [
    root / "README.md",
    root / "CHANGELOG.md",
    root / "CODE_OF_CONDUCT.md",
    root / "CONTRIBUTING.md",
    root / "SECURITY.md",
    root / "scripts" / "README.md",
    root / "app" / "static" / "brand" / "README.md",
    *sorted((root / "docs").rglob("*.md")),
]
for source in markdown_files:
    text = source.read_text(encoding="utf-8")
    for raw_target in re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", text):
        target = raw_target.strip().split("#", 1)[0]
        if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
            continue
        resolved = (source.parent / urllib.parse.unquote(target)).resolve()
        assert resolved.exists(), f"broken documentation link in {source.relative_to(root)}: {raw_target}"

print("PASS: local Docker, persistence, migration, backup/restore, and module contribution documentation is complete and environment-neutral")
