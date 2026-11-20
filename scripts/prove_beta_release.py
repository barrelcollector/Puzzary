"""Run destructive beta install and upgrade proofs in disposable Docker copies."""
from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_GAME_SLUGS = (
    "cross_equations", "crossword", "maze", "minesweeper", "nonogram",
    "spell_it", "sudoku", "takuzu", "word_pyramid", "word_search",
)
PASSWORD = "Beta-release-proof-2026!"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def run(*arguments: str, cwd: Path, capture: bool = False) -> str:
    completed = subprocess.run(
        arguments, cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return completed.stdout.strip() if capture else ""


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def prepare_source(target: Path, source: Path = ROOT) -> None:
    ignored = shutil.ignore_patterns(
        ".git", ".venv", "data", "uploads", "sources", "__pycache__", "*.pyc",
    )
    shutil.copytree(source, target, ignore=ignored)


def archive_ref(reference: str, target: Path) -> None:
    archive = target.parent / "baseline.tar"
    with archive.open("wb") as output:
        subprocess.run(["git", "archive", "--format=tar", reference], cwd=ROOT, check=True, stdout=output)
    target.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(target, filter="data")


def write_environment(source: Path, port: int, version: str | None) -> None:
    template = (source / ".env.example").read_text(encoding="utf-8")
    replacements = {
        "POSTGRES_PASSWORD=change-me": "POSTGRES_PASSWORD=beta-release-db",
        "DATABASE_URL=postgresql+psycopg://puzzary:change-me@db:5432/puzzary":
            "DATABASE_URL=postgresql+psycopg://puzzary:beta-release-db@db:5432/puzzary",
        "SESSION_SECRET=change-this-session-secret": "SESSION_SECRET=beta-release-session-secret",
        "APP_BASE_URL=http://localhost:8790": f"APP_BASE_URL=http://127.0.0.1:{port}",
        "PUZZARY_PORT=8790": f"PUZZARY_PORT={port}",
    }
    for old, new in replacements.items():
        template = template.replace(old, new)
    if version and "PUZZARY_VERSION=" not in template:
        template = f"PUZZARY_VERSION={version}\n" + template
    (source / ".env").write_text(template, encoding="utf-8")


def compose(source: Path, project: str, *arguments: str) -> None:
    run("docker", "compose", "--project-name", project, *arguments, cwd=source)


def wait_for_health(base_url: str, version: str | None = None) -> dict:
    last_error: Exception | None = None
    for _ in range(120):
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=2) as response:
                payload = json.load(response)
            if payload.get("status") == "ok" and (version is None or payload.get("version") == version):
                return payload
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(1)
    raise RuntimeError(f"Puzzary did not become healthy: {last_error}")


def client() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def post_form(opener: urllib.request.OpenerDirector, url: str, fields: dict[str, str]):
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return opener.open(request, timeout=120)


def setup_admin(source: Path, base_url: str, project: str) -> urllib.request.OpenerDirector:
    for _ in range(60):
        try:
            setup_record = run(
                "docker", "compose", "--project-name", project, "exec", "-T", "app",
                "cat", "/data/bootstrap-admin-setup.txt", cwd=source, capture=True,
            )
            break
        except subprocess.CalledProcessError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("First-run setup file was not created")
    setup_line = next(line for line in setup_record.splitlines() if line.startswith("Setup link: "))
    token = urllib.parse.urlsplit(setup_line.removeprefix("Setup link: ")).fragment.removeprefix("token=")
    administrator = client()
    with post_form(administrator, base_url + "/password/setup", {
        "token": token, "password": PASSWORD, "confirm_password": PASSWORD,
    }) as response:
        assert response.geturl().endswith("/library"), response.geturl()
    run(
        "docker", "compose", "--project-name", project, "exec", "-T", "app",
        "test", "!", "-e", "/data/bootstrap-admin-setup.txt", cwd=source,
    )
    return administrator


def login(base_url: str) -> urllib.request.OpenerDirector:
    administrator = client()
    with post_form(administrator, base_url + "/login", {"username": "admin", "password": PASSWORD}) as response:
        assert response.geturl().endswith("/library"), response.geturl()
    return administrator


def multipart_upload(opener: urllib.request.OpenerDirector, url: str) -> None:
    boundary = "----puzzary-beta-release-proof"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"notes\"\r\n\r\nbeta proof\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"proof.png\"\r\n"
        "Content-Type: image/png\r\n\r\n"
    ).encode() + PNG + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with opener.open(request, timeout=120) as response:
        assert "/puzzles/" in response.geturl()


def exercise_install(source: Path, base_url: str, project: str, version: str | None) -> str:
    administrator = setup_admin(source, base_url, project)
    administrator = login(base_url)
    with administrator.open(base_url + "/generate", timeout=30) as response:
        generate_html = response.read().decode()
    for slug in EXPECTED_GAME_SLUGS:
        assert f'value="{slug}"' in generate_html, slug

    with post_form(administrator, base_url + "/web/puzzles/generate", {
        "puzzle_type": "maze", "theme": "Beta Proof", "difficulty": "easy", "seed": "beta-proof",
    }) as response:
        puzzle_url = response.geturl()
    puzzle_id = urllib.parse.urlsplit(puzzle_url).path.rsplit("/", 1)[-1]
    assert puzzle_id

    with administrator.open(base_url + f"/puzzles/{puzzle_id}/print.pdf?size=letter", timeout=120) as response:
        assert response.headers.get_content_type() == "application/pdf"
        assert response.read(4) == b"%PDF"
    multipart_upload(administrator, base_url + f"/web/puzzles/{puzzle_id}/photos")
    uploaded = run(
        "docker", "compose", "--project-name", project, "exec", "-T", "app", "sh", "-c",
        f"find /data/uploads/{puzzle_id} -type f -print -quit", cwd=source, capture=True,
    )
    assert uploaded

    with post_form(administrator, base_url + "/admin/plugins/install-official", {"slug": "word_search"}) as response:
        assert "Installed" in urllib.parse.unquote(response.geturl())
    compose(source, project, "restart", "app")
    wait_for_health(base_url, version)
    administrator = login(base_url)
    with administrator.open(base_url + "/admin/plugins", timeout=30) as response:
        assert "GitHub package" in response.read().decode()
    return puzzle_id


def assert_upgrade(source: Path, base_url: str, project: str, puzzle_id: str, version: str) -> None:
    wait_for_health(base_url, version)
    administrator = login(base_url)
    with administrator.open(base_url + f"/puzzles/{puzzle_id}", timeout=30) as response:
        assert response.status == 200
    uploaded = run(
        "docker", "compose", "--project-name", project, "exec", "-T", "app", "sh", "-c",
        f"find /data/uploads/{puzzle_id} -type f -print -quit", cwd=source, capture=True,
    )
    assert uploaded


def copy_data_with_docker(source: Path, target: Path) -> None:
    run(
        "docker", "run", "--rm",
        "--volume", f"{(source / 'data').resolve()}:/source:ro",
        "--volume", f"{target.resolve()}:/target",
        "alpine:3.20", "sh", "-c", "cp -a /source /target/data",
        cwd=ROOT,
    )


def release_data_ownership(source: Path) -> None:
    data = source / "data"
    if not data.exists() or os.name == "nt":
        return
    run(
        "docker", "run", "--rm", "--volume", f"{data.resolve()}:/data",
        "alpine:3.20", "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/data",
        cwd=ROOT,
    )


def assert_image_version(version: str) -> None:
    label = run(
        "docker", "inspect", "--format", '{{ index .Config.Labels "org.opencontainers.image.version" }}',
        "puzzary", cwd=ROOT, capture=True,
    )
    assert label == version, (label, version)


def prove(reference: str) -> None:
    existing = run("docker", "ps", "-a", "--format", "{{.Names}}", cwd=ROOT, capture=True).splitlines()
    conflict = {"puzzary", "puzzary-db"} & set(existing)
    if conflict:
        raise RuntimeError(f"Refusing to replace existing containers: {sorted(conflict)}")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    with tempfile.TemporaryDirectory(prefix="puzzary-beta-proof-", ignore_cleanup_errors=True) as temporary:
        proof_root = Path(temporary)
        baseline = proof_root / "baseline"
        candidate = proof_root / "candidate"
        clean = proof_root / "clean"
        archive_ref(reference, baseline)
        prepare_source(candidate)
        prepare_source(clean)

        baseline_port = free_port()
        write_environment(baseline, baseline_port, version=None)
        baseline_project = "puzzary-beta-baseline"
        try:
            compose(baseline, baseline_project, "up", "--detach", "--build")
            wait_for_health(f"http://127.0.0.1:{baseline_port}")
            puzzle_id = exercise_install(baseline, f"http://127.0.0.1:{baseline_port}", baseline_project, version=None)
        finally:
            compose(baseline, baseline_project, "down")

        copy_data_with_docker(baseline, candidate)
        release_data_ownership(baseline)
        candidate_port = free_port()
        write_environment(candidate, candidate_port, version)
        candidate_project = "puzzary-beta-upgrade"
        try:
            compose(candidate, candidate_project, "up", "--detach", "--build")
            assert_upgrade(candidate, f"http://127.0.0.1:{candidate_port}", candidate_project, puzzle_id, version)
            assert_image_version(version)
        finally:
            compose(candidate, candidate_project, "down", "--volumes")
            release_data_ownership(candidate)

        clean_port = free_port()
        write_environment(clean, clean_port, version)
        clean_project = "puzzary-beta-clean"
        try:
            compose(clean, clean_project, "up", "--detach", "--build")
            wait_for_health(f"http://127.0.0.1:{clean_port}", version)
            exercise_install(clean, f"http://127.0.0.1:{clean_port}", clean_project, version)
            assert_image_version(version)
        finally:
            compose(clean, clean_project, "down", "--volumes")
            release_data_ownership(clean)

    print(f"PASS: clean install and upgrade from {reference} preserved login, data, all ten games, modules, uploads, printing, and {version} metadata")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="HEAD^", help="Git revision used to create the pre-upgrade data")
    arguments = parser.parse_args()
    prove(arguments.baseline_ref)


if __name__ == "__main__":
    main()
