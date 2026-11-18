"""Install a trusted, versioned Puzzary plugin release archive."""
from __future__ import annotations

import argparse
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


SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def safe_extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            member = PurePosixPath(item.filename)
            if member.is_absolute() or ".." in member.parts:
                raise ValueError(f"unsafe archive path: {item.filename}")
        bundle.extractall(destination)


def package_root(extracted: Path) -> Path:
    if (extracted / "puzzary-plugin.json").is_file():
        return extracted
    candidates = [path.parent for path in extracted.glob("*/puzzary-plugin.json")]
    if len(candidates) != 1:
        raise ValueError("archive must contain exactly one plugin package")
    return candidates[0]


def download_release(url: str, destination: Path) -> None:
    token = os.getenv("PUZZARY_PLUGIN_GITHUB_TOKEN", "").strip()
    headers = {"User-Agent": "Puzzary plugin installer"}
    download_url = url
    if token:
        parts = urlparse(url).path.strip("/").split("/")
        owner, repository, tag = parts[0], parts[1], unquote(parts[4])
        asset_name = unquote("/".join(parts[5:]))
        api_headers = headers | {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
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
        headers |= {
            "Authorization": f"Bearer {token}",
            "Accept": "application/octet-stream",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    request = urllib.request.Request(download_url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def install(url: str, expected_sha256: str, plugin_dir: Path) -> Path:
    expected = expected_sha256.strip().lower()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise ValueError("sha256 must be a 64-character hexadecimal digest")

    parsed_url = urlparse(url)
    if parsed_url.scheme != "https" or parsed_url.hostname != "github.com" or "/releases/download/" not in parsed_url.path:
        raise ValueError("url must be a pinned github.com release download")

    plugin_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="puzzary-plugin-") as temporary:
        work = Path(temporary)
        archive = work / "package.zip"
        download_release(url, archive)
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
        return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Pinned release ZIP URL")
    parser.add_argument("sha256", help="Published SHA-256 digest")
    parser.add_argument("--plugin-dir", default=os.getenv("PUZZARY_PLUGIN_DIR", "/data/plugins"))
    arguments = parser.parse_args()
    installed = install(arguments.url, arguments.sha256, Path(arguments.plugin_dir).resolve())
    print(f"Installed {installed}. Restart Puzzary to activate it.")


if __name__ == "__main__":
    main()
