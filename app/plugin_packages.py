from __future__ import annotations

import importlib.util
import json
import logging
import math
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.version import __version__


LOGGER = logging.getLogger(__name__)
PLUGIN_SCHEMA = "puzzary.plugin.v1"
PLUGIN_API_VERSION = "1"
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SETTING_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
OFFICIAL_CATALOG_PATH = Path(__file__).with_name("official_plugins.json")
PLUGIN_CAPABILITIES = {
    "puzzles.generate", "puzzles.play", "puzzles.print", "puzzles.progress",
    "groups.collaborate", "network.outbound", "filesystem.plugin_data",
}
PLUGIN_SETTING_TYPES = {"text", "textarea", "number", "checkbox", "select", "secret"}


@dataclass(frozen=True)
class PluginPackage:
    slug: str
    name: str
    version: str
    path: Path
    entrypoint: Path | None
    package_type: str = "puzzle"
    templates: Path | None = None
    static: Path | None = None
    homepage: str = ""
    replaces_builtin: bool = False
    author: str = ""
    description: str = ""
    license: str = ""
    capabilities: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    settings: tuple[dict[str, Any], ...] = ()
    min_core_version: str = ""
    max_core_version: str = ""
    content_packs: tuple[dict[str, str], ...] = ()

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
    package_root = package_dir.resolve()
    candidate = (package_root / value).resolve()
    if package_root != candidate and package_root not in candidate.parents:
        raise ValueError(f"{label} must stay inside the plugin package")
    if must_be_file and not candidate.is_file():
        raise ValueError(f"{label} does not exist: {value}")
    if not must_be_file and not candidate.is_dir():
        raise ValueError(f"{label} does not exist: {value}")
    return candidate


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", value.strip())
    if match is None:
        raise ValueError(f"version must start with major.minor.patch: {value}")
    return tuple(int(part) for part in match.groups())


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    cleaned = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
    if len(cleaned) > 100:
        raise ValueError(f"{field} contains too many entries")
    return cleaned


def _settings_schema(value: Any) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("settings must be a list containing at most 50 fields")
    fields: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each settings field must be an object")
        unknown = set(item) - {"name", "label", "type", "required", "default", "options", "help"}
        if unknown:
            raise ValueError(f"unsupported settings fields: {', '.join(sorted(unknown))}")
        name = str(item.get("name", "")).strip()
        field_type = str(item.get("type", "text")).strip()
        if not SETTING_NAME_PATTERN.fullmatch(name) or name in names:
            raise ValueError("plugin setting names must be unique lowercase identifiers")
        if field_type not in PLUGIN_SETTING_TYPES:
            raise ValueError(f"unsupported plugin setting type: {field_type}")
        label = str(item.get("label") or name.replace("_", " ").title()).strip()[:100]
        help_text = str(item.get("help", "")).strip()[:500]
        options = _string_list(item.get("options"), f"settings.{name}.options")
        if field_type == "select" and not options:
            raise ValueError(f"select setting {name} requires options")
        if field_type != "select" and options:
            raise ValueError(f"only select settings can declare options: {name}")
        default = item.get("default", False if field_type == "checkbox" else "")
        if field_type == "secret" and default not in ("", None):
            raise ValueError(f"secret setting {name} cannot have a packaged default")
        if field_type == "checkbox" and not isinstance(default, bool):
            raise ValueError(f"checkbox setting {name} requires a boolean default")
        if field_type == "number" and default not in ("", None):
            if isinstance(default, bool) or not isinstance(default, (int, float)) or not math.isfinite(default):
                raise ValueError(f"number setting {name} requires a finite numeric default")
        if field_type == "select" and default not in options:
            raise ValueError(f"select setting {name} default must be one of its options")
        if field_type in {"text", "textarea"} and (not isinstance(default, str) or len(default) > 4000):
            raise ValueError(f"{field_type} setting {name} requires a string default of at most 4000 characters")
        fields.append({
            "name": name,
            "label": label,
            "type": field_type,
            "required": bool(item.get("required", False)),
            "default": default,
            "options": list(options),
            "help": help_text,
        })
        names.add(name)
    return tuple(fields)


def _content_packs(package_dir: Path, value: Any) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise ValueError("content packages require a non-empty content_packs list")
    packs: list[dict[str, str]] = []
    identifiers: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "kind", "directory", "manifest"}:
            raise ValueError("content pack entries require id, kind, directory, and manifest")
        identifier = str(item["id"]).strip()
        kind = str(item["kind"]).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,127}", identifier) or identifier in identifiers:
            raise ValueError("content pack ids must be unique lowercase identifiers")
        if not SLUG_PATTERN.fullmatch(kind):
            raise ValueError("content pack kind must be a lowercase identifier")
        directory = _child_path(package_dir, str(item["directory"]), "content pack directory")
        manifest = _child_path(package_dir, str(item["manifest"]), "content pack manifest", must_be_file=True)
        if directory != manifest.parent and directory not in manifest.parents:
            raise ValueError("content pack manifest must stay inside its directory")
        packs.append({
            "id": identifier,
            "kind": kind,
            "directory": str(directory),
            "manifest": str(manifest),
        })
        identifiers.add(identifier)
    return tuple(packs)


def read_manifest(package_dir: Path) -> PluginPackage:
    manifest_path = package_dir / "puzzary-plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("missing puzzary-plugin.json") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid puzzary-plugin.json: {exc.msg}") from exc

    if not isinstance(manifest, dict):
        raise ValueError("puzzary-plugin.json must contain a JSON object")

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
    _version_tuple(version)
    min_core_version = str(manifest.get("min_core_version", "")).strip()
    max_core_version = str(manifest.get("max_core_version", "")).strip()
    current_core = _version_tuple(__version__)
    if min_core_version and current_core < _version_tuple(min_core_version):
        raise ValueError(f"plugin requires Puzzary {min_core_version} or newer")
    if max_core_version and current_core > _version_tuple(max_core_version):
        raise ValueError(f"plugin supports Puzzary only through {max_core_version}")
    capabilities = _string_list(manifest.get("capabilities"), "capabilities")
    unknown_capabilities = set(capabilities) - PLUGIN_CAPABILITIES
    if unknown_capabilities:
        raise ValueError(f"unsupported plugin capabilities: {', '.join(sorted(unknown_capabilities))}")
    requires = _string_list(manifest.get("requires"), "requires")
    if any(not SLUG_PATTERN.fullmatch(required) for required in requires) or slug in requires:
        raise ValueError("plugin dependencies must be other valid plugin slugs")
    settings = _settings_schema(manifest.get("settings"))
    package_type = str(manifest.get("package_type", "puzzle")).strip()
    if package_type not in {"puzzle", "content"}:
        raise ValueError("package_type must be puzzle or content")
    entrypoint = None
    if package_type == "puzzle":
        entrypoint = _child_path(package_dir, str(manifest.get("entrypoint", "plugin.py")), "entrypoint", must_be_file=True)
    elif manifest.get("entrypoint"):
        raise ValueError("content packages cannot execute an entrypoint")
    content_packs = _content_packs(package_dir, manifest.get("content_packs"))
    if package_type == "content" and not content_packs:
        raise ValueError("content packages must declare content_packs")
    if package_type == "puzzle" and content_packs:
        raise ValueError("puzzle packages cannot declare content_packs")

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
        package_type=package_type,
        templates=templates,
        static=static,
        homepage=str(manifest.get("homepage", "")),
        replaces_builtin=bool(manifest.get("replaces_builtin", False)),
        author=str(manifest.get("author", "")).strip()[:120],
        description=str(manifest.get("description", "")).strip()[:500],
        license=str(manifest.get("license", "")).strip()[:80],
        capabilities=capabilities,
        requires=requires,
        settings=settings,
        min_core_version=min_core_version,
        max_core_version=max_core_version,
        content_packs=content_packs,
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
            if package.package_type == "content":
                LOADED_PACKAGES[package.slug] = package
                LOGGER.info("Loaded Puzzary content package %s %s", package.slug, package.version)
                continue
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
            compatibility_name = f"app.puzzle_types.{package.slug}"
            sys.modules[compatibility_name] = module
            puzzle_package = sys.modules.get("app.puzzle_types")
            if puzzle_package is not None:
                setattr(puzzle_package, package.slug, module)
            LOGGER.info("Loaded Puzzary plugin %s %s", package.slug, package.version)
        except Exception as exc:
            for registered_slug in registered_slugs:
                registry.pop(registered_slug, None)
            if package is not None and package.slug in registry and registry.get(package.slug) is not previous:
                registry.pop(package.slug, None)
            if previous is not None and package is not None:
                registry[package.slug] = previous
            if package is not None:
                compatibility_name = f"app.puzzle_types.{package.slug}"
                sys.modules.pop(compatibility_name, None)
                puzzle_package = sys.modules.get("app.puzzle_types")
                if puzzle_package is not None and hasattr(puzzle_package, package.slug):
                    delattr(puzzle_package, package.slug)
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


def plugin_content_packs() -> list[dict[str, str]]:
    return [
        {**pack, "package_slug": package.slug, "package_version": package.version}
        for package in LOADED_PACKAGES.values()
        for pack in package.content_packs
    ]


def official_plugin_catalog() -> dict[str, Any]:
    catalog = json.loads(OFFICIAL_CATALOG_PATH.read_text(encoding="utf-8"))
    if catalog.get("schema") != "puzzary.plugin.catalog.v1":
        raise ValueError("official plugin catalog schema is invalid")
    plugins = catalog.get("plugins")
    if not isinstance(plugins, list):
        raise ValueError("official plugin catalog must contain a plugins list")
    seen: set[str] = set()
    for plugin in plugins:
        if not isinstance(plugin, dict) or not SLUG_PATTERN.fullmatch(str(plugin.get("slug", ""))):
            raise ValueError("official plugin catalog contains an invalid slug")
        slug = str(plugin["slug"])
        if slug in seen:
            raise ValueError(f"official plugin catalog contains duplicate slug {slug}")
        seen.add(slug)
        if plugin.get("package_type", "puzzle") not in {"puzzle", "content"}:
            raise ValueError(f"official plugin {slug} has an invalid package type")
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
            install_metadata_path = package_dir / ".puzzary-install.json"
            if install_metadata_path.is_file():
                try:
                    metadata = json.loads(install_metadata_path.read_text(encoding="utf-8"))
                    data["install_source"] = str(metadata.get("source", ""))[:2048]
                    data["installed_sha256"] = str(metadata.get("sha256", ""))[:64]
                except (OSError, json.JSONDecodeError):
                    data["install_source"] = ""
                    data["installed_sha256"] = ""
            else:
                data["install_source"] = ""
                data["installed_sha256"] = ""
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
        "content_packs": plugin_content_packs(),
        "installed": installed,
        "rollbacks": rollbacks,
        "official_catalog": official_plugin_catalog(),
        "github_access_configured": bool(os.getenv("PUZZARY_PLUGIN_GITHUB_TOKEN", "").strip()),
        "restart_required": (
            any(package["restart_required"] for package in installed)
            or bool(set(LOADED_PACKAGES) - {package["slug"] for package in installed})
        ),
        "trust_boundary": "Plugin packages execute trusted server-side Python with the same process privileges as Puzzary.",
        "supported_capabilities": sorted(PLUGIN_CAPABILITIES),
        "errors": dict(PLUGIN_LOAD_ERRORS) | installed_errors | rollback_errors,
    }
