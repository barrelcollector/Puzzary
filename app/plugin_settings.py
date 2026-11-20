from __future__ import annotations

import json
import math
import os
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import AppSetting
from app.oidc import OIDCError, decrypt_secret, encrypt_secret
from app.plugin_packages import PluginPackage, plugin_root, read_manifest


SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-local-session-secret")
SETTING_PREFIX = "plugin_config:"


def _key(slug: str) -> str:
    return f"{SETTING_PREFIX}{slug}"


def read_plugin_settings(db: Session, slug: str) -> dict[str, Any]:
    setting = db.get(AppSetting, _key(slug))
    if setting is None or not setting.value:
        return {}
    try:
        value = json.loads(decrypt_secret(setting.value, SESSION_SECRET))
    except (OIDCError, json.JSONDecodeError) as error:
        raise ValueError(f"Saved settings for {slug} cannot be decrypted or decoded.") from error
    if not isinstance(value, dict):
        raise ValueError(f"Saved settings for {slug} are invalid.")
    return value


def plugin_settings_for_package(db: Session, package: PluginPackage) -> dict[str, Any]:
    saved = read_plugin_settings(db, package.slug)
    return {
        field["name"]: saved.get(field["name"], field.get("default"))
        for field in package.settings
    }


def plugin_settings_view(db: Session, package: PluginPackage) -> dict[str, Any]:
    values = plugin_settings_for_package(db, package)
    secret_configured: dict[str, bool] = {}
    for field in package.settings:
        if field["type"] == "secret":
            name = field["name"]
            secret_configured[name] = bool(values.get(name))
            values[name] = ""
    return {"values": values, "secret_configured": secret_configured}


def validate_plugin_settings(
    package: PluginPackage,
    submitted: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = previous or {}
    known = {field["name"] for field in package.settings}
    unknown = set(submitted) - known
    if unknown:
        raise ValueError(f"Unknown plugin settings: {', '.join(sorted(unknown))}")
    cleaned: dict[str, Any] = {}
    for field in package.settings:
        name = field["name"]
        field_type = field["type"]
        raw = submitted.get(name, False if field_type == "checkbox" else "")
        if field_type == "checkbox":
            value = raw if isinstance(raw, bool) else str(raw).lower() in {"1", "true", "on", "yes"}
        elif field_type == "number":
            if raw in ("", None):
                value = ""
            else:
                try:
                    number = float(raw)
                except (TypeError, ValueError) as error:
                    raise ValueError(f"{field['label']} must be a number.") from error
                if not math.isfinite(number):
                    raise ValueError(f"{field['label']} must be a finite number.")
                value = int(number) if number.is_integer() else number
        else:
            value = str(raw or "").strip()
            if len(value) > 4000:
                raise ValueError(f"{field['label']} is too long.")
            if field_type == "secret" and not value:
                value = previous.get(name, "")
            if field_type == "select" and value not in field["options"]:
                raise ValueError(f"Choose a valid value for {field['label']}.")
        if field.get("required") and value in ("", None, False):
            raise ValueError(f"{field['label']} is required.")
        cleaned[name] = value
    return cleaned


def save_plugin_settings(db: Session, slug: str, values: dict[str, Any]) -> None:
    encrypted = encrypt_secret(json.dumps(values, separators=(",", ":")), SESSION_SECRET)
    setting = db.get(AppSetting, _key(slug))
    if setting is None:
        db.add(AppSetting(key=_key(slug), value=encrypted))
    else:
        setting.value = encrypted


def configured_plugin_settings(slug: str) -> dict[str, Any]:
    """Return decrypted settings to an installed plugin at runtime."""
    package = read_manifest(plugin_root() / slug)
    with SessionLocal() as db:
        return plugin_settings_for_package(db, package)
