from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse
from urllib.error import HTTPError, URLError


SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 250 * 1024 * 1024
GITHUB_API_BASE = "https://api.github.com"
OFFICIAL_PLUGIN_REPOSITORY = "barrelcollector/puzzary-official-plugins"


def github_headers(token: str, *, asset: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": "Puzzary plugin installer",
        "Accept": "application/octet-stream" if asset else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def test_github_access(token: str, repository: str = OFFICIAL_PLUGIN_REPOSITORY) -> dict:
    clean_token = token.strip()
    if not clean_token:
        raise ValueError("No private GitHub credential is configured.")
    request = urllib.request.Request(
        f"{GITHUB_API_BASE}/repos/{repository}",
        headers=github_headers(clean_token),
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as error:
        if error.code == 401:
            raise ValueError("GitHub rejected the credential. Replace it and try again.") from error
        if error.code == 403:
            raise ValueError("GitHub accepted the request but denied repository access.") from error
        if error.code == 404:
            raise ValueError("The credential cannot access the GitHub repository used for the access check.") from error
        raise ValueError(f"GitHub access check failed with status {error.code}.") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ValueError("GitHub could not be reached to test repository access.") from error
    if payload.get("full_name") != repository:
        raise ValueError("GitHub returned an unexpected repository during the access check.")
    return {
        "repository": repository,
        "private": bool(payload.get("private")),
    }


def safe_extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        if sum(item.file_size for item in bundle.infolist()) > MAX_EXTRACTED_BYTES:
            raise ValueError("plugin archive expands beyond the 250 MB limit")
        for item in bundle.infolist():
            member = PurePosixPath(item.filename)
            if member.is_absolute() or ".." in member.parts or "\\" in item.filename or any(":" in part for part in member.parts):
                raise ValueError(f"unsafe archive path: {item.filename}")
        bundle.extractall(destination)


def package_root(extracted: Path) -> Path:
    if (extracted / "puzzary-plugin.json").is_file():
        return extracted
    candidates = [path.parent for path in extracted.glob("*/puzzary-plugin.json")]
    if len(candidates) != 1:
        raise ValueError("archive must contain exactly one plugin package")
    return candidates[0]


def download_release(url: str, destination: Path, github_token: str | None = None) -> None:
    token = (github_token if github_token is not None else os.getenv("PUZZARY_PLUGIN_GITHUB_TOKEN", "")).strip()
    headers = {"User-Agent": "Puzzary plugin installer"}
    download_url = url
    if token:
        parts = urlparse(url).path.strip("/").split("/")
        owner, repository, tag = parts[0], parts[1], unquote(parts[4])
        asset_name = unquote("/".join(parts[5:]))
        api_headers = github_headers(token)
        release_request = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repository}/releases/tags/{tag}",
            headers=api_headers,
        )
        with urllib.request.urlopen(release_request, timeout=60) as response:
            release = json.load(response)
        asset = next((item for item in release.get("assets", []) if item.get("name") == asset_name), None)
        if asset is None:
            raise ValueError(f"release does not contain asset {asset_name}")
        download_url = asset["url"]
        headers = github_headers(token, asset=True)
    request = urllib.request.Request(download_url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("plugin archive exceeds the 100 MB download limit")
            output.write(chunk)


def install_plugin_release(
    url: str,
    expected_sha256: str,
    plugin_dir: Path,
    github_token: str | None = None,
) -> tuple[Path, dict]:
    expected = expected_sha256.strip().lower()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise ValueError("sha256 must be a 64-character hexadecimal digest")

    parsed_url = urlparse(url)
    url_parts = parsed_url.path.strip("/").split("/")
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "github.com"
        or len(url_parts) < 6
        or url_parts[2:4] != ["releases", "download"]
        or any(not part for part in (url_parts[0], url_parts[1], url_parts[4], url_parts[5]))
    ):
        raise ValueError("url must be a pinned github.com release download")

    plugin_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="puzzary-plugin-") as temporary:
        work = Path(temporary)
        archive = work / "package.zip"
        download_release(url, archive, github_token)
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"archive digest mismatch: expected {expected}, received {actual}")

        extracted = work / "extracted"
        extracted.mkdir()
        safe_extract(archive, extracted)
        source = package_root(extracted)
        manifest = json.loads((source / "puzzary-plugin.json").read_text(encoding="utf-8"))
        if manifest.get("schema") != "puzzary.plugin.v1" or str(manifest.get("api_version")) != "1":
            raise ValueError("package does not target Puzzary plugin API version 1")
        slug = str(manifest.get("slug", "")).strip()
        if not SLUG_PATTERN.fullmatch(slug):
            raise ValueError("package slug must use lowercase letters, numbers, and underscores")
        target = plugin_dir / slug
        staged = plugin_dir / f".{slug}.staged"
        rollback = plugin_dir / f".{slug}.rollback"
        if staged.exists():
            shutil.rmtree(staged)
        shutil.copytree(source, staged)
        if rollback.exists():
            shutil.rmtree(rollback)
        if target.exists():
            os.replace(target, rollback)
        try:
            os.replace(staged, target)
        except Exception:
            if rollback.exists() and not target.exists():
                os.replace(rollback, target)
            raise
        return target, manifest


def restore_plugin_rollback(slug: str, plugin_dir: Path) -> tuple[Path, dict]:
    if not SLUG_PATTERN.fullmatch(slug):
        raise ValueError("plugin slug is invalid")
    target = plugin_dir / slug
    rollback = plugin_dir / f".{slug}.rollback"
    swap = plugin_dir / f".{slug}.swap"
    if not rollback.is_dir():
        raise ValueError(f"no rollback package is available for {slug}")
    if swap.exists():
        raise ValueError(f"unfinished rollback swap exists for {slug}; inspect it before retrying")
    manifest = json.loads((rollback / "puzzary-plugin.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "puzzary.plugin.v1" or manifest.get("slug") != slug:
        raise ValueError("rollback manifest does not match the requested plugin")
    if target.exists():
        os.replace(target, swap)
    try:
        os.replace(rollback, target)
    except Exception:
        if swap.exists() and not target.exists():
            os.replace(swap, target)
        raise
    if swap.exists():
        try:
            os.replace(swap, rollback)
        except Exception:
            os.replace(target, rollback)
            os.replace(swap, target)
            raise
    return target, manifest
