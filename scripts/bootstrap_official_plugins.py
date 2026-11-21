"""Install the checksummed official catalog into a disposable plugin directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "app" / "official_plugins.json"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def safe_extract(archive: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            member = PurePosixPath(item.filename)
            if member.is_absolute() or ".." in member.parts or "\\" in item.filename:
                raise ValueError(f"unsafe archive path: {item.filename}")
        bundle.extractall(destination)
    manifests = list(destination.glob("*/puzzary-plugin.json"))
    if len(manifests) != 1:
        raise ValueError("official archive must contain exactly one package")
    return manifests[0].parent


def install(destination: Path, archive_directory: Path | None = None) -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="puzzary-official-bootstrap-") as temporary:
        work = Path(temporary)
        for entry in catalog["plugins"]:
            filename = Path(urlparse(entry["url"]).path).name
            archive = work / filename
            local_archive = archive_directory / filename if archive_directory else None
            if local_archive is not None and local_archive.is_file():
                shutil.copy2(local_archive, archive)
            else:
                request = urllib.request.Request(entry["url"], headers={"User-Agent": "Puzzary QA bootstrap"})
                with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as output:  # nosec B310
                    shutil.copyfileobj(response, output)
            actual = digest(archive)
            if actual != entry["sha256"]:
                raise ValueError(f"checksum mismatch for {entry['slug']}: {actual}")
            extracted = work / f"extract-{entry['slug']}"
            extracted.mkdir()
            package = safe_extract(archive, extracted)
            manifest = json.loads((package / "puzzary-plugin.json").read_text(encoding="utf-8"))
            if manifest.get("slug") != entry["slug"] or manifest.get("version") != entry["version"]:
                raise ValueError(f"catalog metadata mismatch for {entry['slug']}")
            target = destination / entry["slug"]
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(package, target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--archive-dir", type=Path)
    arguments = parser.parse_args()
    install(arguments.destination.resolve(), arguments.archive_dir.resolve() if arguments.archive_dir else None)
