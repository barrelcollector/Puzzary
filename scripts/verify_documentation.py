import re
from pathlib import Path


root = Path(__file__).resolve().parents[1]
readme = (root / "README.md").read_text(encoding="utf-8")
deployment = (root / "docs/docker-deployment.md").read_text(encoding="utf-8")
modules = (root / "docs/puzzle-modules.md").read_text(encoding="utf-8")
env_example = (root / ".env.example").read_text(encoding="utf-8")
docs = "\n".join(
    path.read_text(encoding="utf-8")
    for path in [root / "README.md", *sorted((root / "docs").glob("*.md"))]
)

assert "docs/docker-deployment.md" in readme
assert "docs/puzzle-modules.md" in readme
for marker in (
    "docker compose up -d --build",
    "./data/postgres",
    "built-in, idempotent schema",
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

assert "replace every change-* value" in env_example
assert not re.search(r"\b(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)", docs)

print("PASS: local Docker, persistence, migration, backup/restore, and module contribution documentation is complete and environment-neutral")
