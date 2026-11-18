from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)
PLUGIN_SCHEMA = "puzzary.plugin.v1"
PLUGIN_API_VERSION = "1"
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
OFFICIAL_CATALOG_PATH = Path(__file__).with_name("official_plugins.json")


@dataclass(frozen=True)
class PluginPackage:
    slug: str
    name: str
    version: str
    path: Path
    entrypoint: Path
    templates: Path | None = None
    static: Path | None = None
    homepage: str = ""
    replaces_builtin: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("path", "entrypoint", "templates", "static"):
            value = data[key]
            data[key] = str(value) if value else None
        return data


LOADED_PACKAGES: dict[str, PluginPackage] = {}
PLUGIN_LOAD_ERRORS: dict[str, str] = {}


def plugin_root() -> Path:
    return Path(os.getenv("PUZZARY_PLUGIN_DIR", "/data/plugins")).expanduser().resolve()


def _child_path(package_dir: Path, value: str, label: str, *, must_be_file: bool = False) -> Path:
    candidate = (package_dir / value).resolve()
    if package_dir != candidate and package_dir not in candidate.parents:
        raise ValueError(f"{label} must stay inside the plugin package")
    if must_be_file and not candidate.is_file():
        raise ValueError(f"{label} does not exist: {value}")
    if not must_be_file and not candidate.is_dir():
        raise ValueError(f"{label} does not exist: {value}")
    return candidate


def read_manifest(package_dir: Path) -> PluginPackage:
    manifest_path = package_dir / "puzzary-plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("missing puzzary-plugin.json") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid puzzary-plugin.json: {exc.msg}") from exc

    if manifest.get("schema") != PLUGIN_SCHEMA:
        raise ValueError(f"schema must be {PLUGIN_SCHEMA}")
    if str(manifest.get("api_version", "")) != PLUGIN_API_VERSION:
        raise ValueError(f"api_version must be {PLUGIN_API_VERSION}")
    slug = str(manifest.get("slug", "")).strip()
    if not SLUG_PATTERN.fullmatch(slug):
        raise ValueError("slug must use lowercase letters, numbers, and underscores")
    version = str(manifest.get("version", "")).strip()
    if not version:
        raise ValueError("version is required")
    entrypoint = _child_path(package_dir, str(manifest.get("entrypoint", "plugin.py")), "entrypoint", must_be_file=True)

    templates = None
    if manifest.get("templates"):
        templates = _child_path(package_dir, str(manifest["templates"]), "templates")
    static = None
    if manifest.get("static"):
        static = _child_path(package_dir, str(manifest["static"]), "static")

    return PluginPackage(
        slug=slug,
        name=str(manifest.get("name") or slug.replace("_", " ").title()),
        version=version,
        path=package_dir,
        entrypoint=entrypoint,
        templates=templates,
        static=static,
        homepage=str(manifest.get("homepage", "")),
        replaces_builtin=bool(manifest.get("replaces_builtin", False)),
    )


def load_external_plugin_packages(registry: dict[str, Any]) -> None:
    root = plugin_root()
    if not root.is_dir():
        return

    for package_dir in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")):
        package_key = package_dir.name
        previous = None
        package: PluginPackage | None = None
        registered_slugs: set[str] = set()
        try:
            package = read_manifest(package_dir)
            package_key = package.slug
            if package.slug in LOADED_PACKAGES:
                raise ValueError(f"duplicate external plugin slug: {package.slug}")
            previous = registry.get(package.slug)
            if previous is not None and not package.replaces_builtin:
                raise ValueError(
                    f"puzzle type {package.slug} is already built in; set replaces_builtin to migrate it"
                )
            if previous is not None:
                del registry[package.slug]
            registry_before = set(registry)

            module_name = f"puzzary_external_{package.slug}_{package.version.replace('.', '_').replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(module_name, package.entrypoint)
            if spec is None or spec.loader is None:
                raise ValueError("entrypoint could not be loaded")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            registered_slugs = set(registry) - registry_before
            if registered_slugs != {package.slug}:
                raise ValueError(
                    f"entrypoint must register only {package.slug}; registered {sorted(registered_slugs)}"
                )
            registered = registry.get(package.slug)
            if registered is None:
                raise ValueError(f"entrypoint did not register puzzle type {package.slug}")
            LOADED_PACKAGES[package.slug] = package
            LOGGER.info("Loaded Puzzary plugin %s %s", package.slug, package.version)
        except Exception as exc:
            for registered_slug in registered_slugs:
                registry.pop(registered_slug, None)
            if package is not None and package.slug in registry and registry.get(package.slug) is not previous:
                registry.pop(package.slug, None)
            if previous is not None and package is not None:
                registry[package.slug] = previous
            PLUGIN_LOAD_ERRORS[package_key] = str(exc)
            LOGGER.exception("Could not load Puzzary plugin package %s", package_dir)


def plugin_template_directories() -> list[Path]:
    return [package.templates for package in LOADED_PACKAGES.values() if package.templates]


def plugin_static_directories() -> dict[str, Path]:
    return {
        slug: package.static
        for slug, package in LOADED_PACKAGES.items()
        if package.static is not None
    }


def official_plugin_catalog() -> dict[str, Any]:
    catalog = json.loads(OFFICIAL_CATALOG_PATH.read_text(encoding="utf-8"))
    if catalog.get("schema") != "puzzary.plugin.catalog.v1":
        raise ValueError("official plugin catalog schema is invalid")
    plugins = catalog.get("plugins")
    if not isinstance(plugins, list):
        raise ValueError("official plugin catalog must contain a plugins list")
    for plugin in plugins:
        if not isinstance(plugin, dict) or not SLUG_PATTERN.fullmatch(str(plugin.get("slug", ""))):
            raise ValueError("official plugin catalog contains an invalid slug")
        digest = str(plugin.get("sha256", ""))
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
            raise ValueError(f"official plugin {plugin['slug']} has an invalid digest")
    return catalog


def installed_plugin_packages() -> tuple[list[dict[str, Any]], dict[str, str]]:
    installed: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    root = plugin_root()
    if not root.is_dir():
        return installed, errors
    for package_dir in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")):
        try:
            package = read_manifest(package_dir)
            data = package.as_dict()
            loaded = LOADED_PACKAGES.get(package.slug)
            data["loaded"] = loaded is not None and loaded.version == package.version
            data["loaded_version"] = loaded.version if loaded is not None else None
            data["restart_required"] = loaded is None or loaded.version != package.version
            installed.append(data)
        except Exception as exc:
            errors[package_dir.name] = str(exc)
    return installed, errors


def rollback_plugin_packages() -> tuple[list[dict[str, Any]], dict[str, str]]:
    rollbacks: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    root = plugin_root()
    if not root.is_dir():
        return rollbacks, errors
    for package_dir in sorted(path for path in root.glob(".*.rollback") if path.is_dir()):
        try:
            rollbacks.append(read_manifest(package_dir).as_dict())
        except Exception as exc:
            errors[package_dir.name] = str(exc)
    return rollbacks, errors


def package_status() -> dict[str, Any]:
    installed, installed_errors = installed_plugin_packages()
    rollbacks, rollback_errors = rollback_plugin_packages()
    return {
        "schema": PLUGIN_SCHEMA,
        "api_version": PLUGIN_API_VERSION,
        "directory": str(plugin_root()),
        "packages": [package.as_dict() for package in LOADED_PACKAGES.values()],
        "installed": installed,
        "rollbacks": rollbacks,
        "official_catalog": official_plugin_catalog(),
        "github_access_configured": bool(os.getenv("PUZZARY_PLUGIN_GITHUB_TOKEN", "").strip()),
        "restart_required": any(package["restart_required"] for package in installed),
        "errors": dict(PLUGIN_LOAD_ERRORS) | installed_errors | rollback_errors,
    }
