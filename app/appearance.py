"""Validated, portable appearance themes kept separate from puzzle subject themes."""
from __future__ import annotations

import json
import math
import re
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AppSetting, AppearanceTheme, User


SCHEMA = "puzzary-theme/v1"
BUILTIN_SLUGS = {"system", "light", "dark"}
COLOR_TOKENS = (
    "bg", "panel", "ink", "muted", "line", "accent", "accent_text", "focus",
    "success", "success_bg", "danger", "danger_bg", "surface_muted", "puzzle_cell",
    "puzzle_grid", "player_1", "player_2", "player_3", "player_4", "player_5", "player_6",
)
TOKEN_KEYS = set(COLOR_TOKENS) | {"font", "radius", "density"}
FONT_STACKS = {
    "system": 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    "serif": 'Georgia, "Times New Roman", serif',
    "mono": 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
}
LIGHT_TOKENS = {
    "bg":"#f5f5f2", "panel":"#ffffff", "ink":"#151515", "muted":"#62625c", "line":"#d7d5cc",
    "accent":"#1f6f62", "accent_text":"#ffffff", "focus":"#1268a5", "success":"#175f50",
    "success_bg":"#e3f2ec", "danger":"#7c2727", "danger_bg":"#f8e7e4", "surface_muted":"#fbfbf8",
    "puzzle_cell":"#ffffff", "puzzle_grid":"#151515", "player_1":"#197667", "player_2":"#b33d56",
    "player_3":"#256ca6", "player_4":"#7951a8", "player_5":"#986013", "player_6":"#167e8c",
    "font":"system", "radius":6, "density":1.0,
}
DARK_TOKENS = {
    "bg":"#101512", "panel":"#1b221e", "ink":"#f3f7f4", "muted":"#b5c1ba", "line":"#46514a",
    "accent":"#64cfb8", "accent_text":"#08261f", "focus":"#8dc8ff", "success":"#78d9bd",
    "success_bg":"#173a31", "danger":"#ffaaa2", "danger_bg":"#4a2424", "surface_muted":"#242c28",
    "puzzle_cell":"#222b26", "puzzle_grid":"#dce7e0", "player_1":"#62d6bd", "player_2":"#ff879d",
    "player_3":"#79bfff", "player_4":"#c8a0ff", "player_5":"#f1bd65", "player_6":"#65d4e5",
    "font":"system", "radius":6, "density":1.0,
}


def _rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def _luminance(value: str) -> float:
    channels = [part / 255 for part in _rgb(value)]
    converted = [part / 12.92 if part <= .04045 else ((part + .055) / 1.055) ** 2.4 for part in channels]
    return .2126 * converted[0] + .7152 * converted[1] + .0722 * converted[2]


def contrast(first: str, second: str) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + .05) / (low + .05)


def _text(value: Any, field: str, maximum: int, required: bool = False) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"Theme {field} is required")
    if len(result) > maximum or any(ord(char) < 32 for char in result):
        raise ValueError(f"Theme {field} is invalid")
    return result


def validate_manifest(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Theme package must be a JSON object")
    allowed = {"schema", "slug", "name", "version", "author", "description", "tokens"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"Unsupported theme fields: {', '.join(sorted(unknown))}")
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"Theme schema must be {SCHEMA}")
    slug = _text(payload.get("slug"), "slug", 64, True).lower()
    if slug in BUILTIN_SLUGS or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", slug):
        raise ValueError("Theme slug must use 2-64 lowercase letters, numbers, or hyphens and cannot replace a built-in theme")
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("Theme tokens must be an object")
    unknown_tokens = set(tokens) - TOKEN_KEYS
    if unknown_tokens:
        raise ValueError(f"Unsupported theme tokens: {', '.join(sorted(unknown_tokens))}")
    normalized = dict(LIGHT_TOKENS)
    normalized.update(tokens)
    for key in COLOR_TOKENS:
        value = str(normalized.get(key, "")).lower()
        if not re.fullmatch(r"#[0-9a-f]{6}", value):
            raise ValueError(f"Theme token {key} must be a six-digit hex color")
        normalized[key] = value
    if normalized.get("font") not in FONT_STACKS:
        raise ValueError("Theme font must be system, serif, or mono")
    try:
        radius = float(normalized.get("radius", 6))
        normalized["density"] = float(normalized.get("density", 1))
    except (TypeError, ValueError) as error:
        raise ValueError("Theme radius and density must be numbers") from error
    if not math.isfinite(radius) or not radius.is_integer() or not math.isfinite(normalized["density"]):
        raise ValueError("Theme radius must be a whole number and density must be finite")
    normalized["radius"] = int(radius)
    if not 0 <= normalized["radius"] <= 24 or not .8 <= normalized["density"] <= 1.25:
        raise ValueError("Theme radius must be 0-24 and density must be 0.8-1.25")
    checks = (("ink", "bg", 4.5), ("ink", "panel", 4.5), ("muted", "bg", 4.5),
              ("accent_text", "accent", 4.5), ("danger", "danger_bg", 4.5))
    for foreground, background, minimum in checks:
        if contrast(normalized[foreground], normalized[background]) < minimum:
            raise ValueError(f"Theme colors {foreground} and {background} need at least {minimum}:1 contrast")
    players = [normalized[f"player_{index}"] for index in range(1, 7)]
    if len(set(players)) != len(players):
        raise ValueError("Group Solve player colors must be distinct")
    if any(math.dist(_rgb(first), _rgb(second)) < 25 for index, first in enumerate(players) for second in players[index + 1:]):
        raise ValueError("Group Solve player colors are too visually similar")
    if any(contrast(color, normalized["bg"]) < 3 for color in players):
        raise ValueError("Every Group Solve player color needs at least 3:1 contrast against the page background")
    return {
        "schema": SCHEMA,
        "slug": slug,
        "name": _text(payload.get("name"), "name", 80, True),
        "version": _text(payload.get("version") or "1.0.0", "version", 32, True),
        "author": _text(payload.get("author"), "author", 120),
        "description": _text(payload.get("description"), "description", 500),
        "tokens": normalized,
    }


def builtins() -> list[dict]:
    return [
        {"slug":"system", "name":"Follow system", "description":"Use the device light or dark preference.", "builtin":True},
        {"slug":"light", "name":"Light", "description":"Puzzary's built-in light appearance.", "builtin":True, "tokens":LIGHT_TOKENS},
        {"slug":"dark", "name":"Dark", "description":"Puzzary's built-in dark appearance.", "builtin":True, "tokens":DARK_TOKENS},
    ]


def installed(db, include_disabled: bool = False) -> list[AppearanceTheme]:
    statement = select(AppearanceTheme).order_by(AppearanceTheme.name, AppearanceTheme.slug)
    if not include_disabled:
        statement = statement.where(AppearanceTheme.enabled == True)  # noqa: E712
    return list(db.scalars(statement))


def normalized_installed(db, include_disabled: bool = False) -> list[tuple[AppearanceTheme, dict]]:
    result = []
    for theme in installed(db, include_disabled):
        try:
            result.append((theme, validate_manifest(theme.manifest)))
        except ValueError:
            continue
    return result


def available(db) -> list[dict]:
    result = builtins()
    result.extend({**manifest, "builtin":False, "enabled":theme.enabled} for theme, manifest in normalized_installed(db))
    return result


def default_slug(db) -> str:
    setting = db.get(AppSetting, "appearance_default")
    requested = setting.value if setting and setting.value else "system"
    active = BUILTIN_SLUGS | {theme.slug for theme, _ in normalized_installed(db)}
    return requested if requested in active else "system"


def resolve(db, user: User | None) -> dict:
    requested = (user.appearance_theme or "").strip() if user else ""
    active_custom = {theme.slug: manifest for theme, manifest in normalized_installed(db)}
    slug = requested if requested in BUILTIN_SLUGS or requested in active_custom else default_slug(db)
    tokens = LIGHT_TOKENS if slug == "light" else DARK_TOKENS if slug == "dark" else (
        active_custom[slug]["tokens"] if slug in active_custom else None
    )
    scheme = "light dark" if slug == "system" else "dark" if tokens and _luminance(tokens["bg"]) < .35 else "light"
    return {"slug":slug, "preference":requested or "server", "tokens":tokens, "style":style(tokens) if tokens else "", "scheme":scheme}


def style(tokens: dict | None) -> str:
    if not tokens:
        return ""
    declarations = [f"--{key.replace('_', '-')}:{tokens[key]}" for key in COLOR_TOKENS]
    declarations.extend((f"--font:{FONT_STACKS[tokens['font']]}", f"--radius:{tokens['radius']}px", f"--density:{tokens['density']}"))
    return ";".join(declarations)


def package_style(payload: Any) -> str:
    try:
        return style(validate_manifest(payload)["tokens"])
    except ValueError:
        return ""


def template_context(request) -> dict:
    with SessionLocal() as db:
        user = None
        user_id = request.session.get("user_id") if hasattr(request, "session") else None
        if user_id:
            candidate = db.get(User, int(user_id))
            if candidate and candidate.is_active and int(request.session.get("session_version", 0)) == int(candidate.session_version or 1):
                user = candidate
        return {"appearance":resolve(db, user)}


def decode_package(raw: bytes) -> dict:
    if len(raw) > 65536:
        raise ValueError("Theme package is larger than 64 KB")
    try:
        return validate_manifest(json.loads(raw.decode("utf-8-sig")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Theme package is not valid UTF-8 JSON") from error
