from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import uuid
import hashlib
import hmac
import math
import secrets
import smtplib
import ssl
import time
import threading
import tempfile
import unicodedata
from collections import deque
from copy import deepcopy
from email.message import EmailMessage
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from types import SimpleNamespace

import qrcode
from qrcode.image.pure import PyPNGImage
from qrcode.image.svg import SvgPathImage
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader, TemplateNotFound
from itsdangerous import URLSafeTimedSerializer, BadSignature
from playwright.sync_api import Error as PlaywrightError
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from sqlalchemy import delete, desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.content import THEMES
from app.transport import load_transport_config
from app.clues import properties_with_clues, stored_definition_clues
from app.activity_events import event_summary, record_progress_transition, record_puzzle_event
from app.database import SessionLocal, get_db
from app.dictionary_imports import ImportedWord, fetch_dictionary_source, parse_imported_words
from app.dictionary_queries import dictionary_statement, tag_usage_counts, word_bank_entry_counts
from app.print_pdf import render_pdf, render_png
from app.models import (
    AppSetting,
    AppearanceTheme,
    DictionaryDefinition,
    DictionaryImportSource,
    DictionaryWord,
    Puzzle,
    PuzzleActivityEvent,
    PuzzleAttempt,
    PuzzleCategory,
    PuzzleDictionary,
    PuzzleHint,
    PuzzleGuestLink,
    PuzzleGuestSession,
    PuzzleSeries,
    PuzzleSeriesEntry,
    PuzzlePhoto,
    PuzzleProgress,
    PublishedIssue,
    PuzzleTag,
    OIDCAuthAttempt,
    OIDCIdentity,
    OIDCProvider,
    PermissionRole,
    User,
    WordBank,
    WordBankWord,
)
from app.appearance import (
    BUILTIN_SLUGS,
    available as appearance_options,
    decode_package,
    default_slug as appearance_default_slug,
    package_style as appearance_package_style,
    template_context as appearance_template_context,
)
from app.puzzles import fallback_preview, get_puzzle_type, list_puzzle_types
from app.plugin_packages import (
    LOADED_PACKAGES,
    official_plugin_catalog,
    package_status,
    plugin_root,
    plugin_static_directories,
    plugin_template_directories,
    read_manifest,
)
from app.plugin_installer import (
    MAX_ARCHIVE_BYTES,
    install_plugin_archive,
    install_plugin_release,
    restore_plugin_rollback,
    test_github_access,
    uninstall_plugin_package,
)
from app.plugin_settings import (
    plugin_settings_for_package,
    plugin_settings_view,
    save_plugin_settings,
    validate_plugin_settings,
)
from app.user_activity import account_activity, progress_states
from app.permissions import (
    authorize_request, initialize_permissions, require as require_permission,
    can_view, can_discover, puzzle_is_unlisted, puzzle_is_published,
    puzzle_is_scheduled, puzzle_publication_state, require_owned, can_owned,
    audit, lock_permissions, protect_last_admin,
    register_route_permission, PLAYER,
)
from app.schemas import (
    DictionaryDefinitionPatch,
    DictionaryDefinitionWrite,
    DictionaryImportRequest,
    DictionaryImportSourceWrite,
    DictionaryWordPatch,
    DictionaryWordWrite,
    PuzzleTagPatch,
    PuzzleTagWrite,
    PuzzleAttemptCreate,
    PuzzleActivityCreate,
    PuzzleAttemptResponse,
    PuzzleGenerateRequest,
    PuzzleProgressResponse,
    PuzzleProgressUpdate,
    OfflineProgressSync,
    PuzzlePlayAction,
    PuzzleImportRequest,
    PluginInstallRequest,
    OfficialPluginInstallRequest,
    PluginEnabledRequest,
    PluginGitHubAccessWrite,
    PluginSettingsWrite,
    PuzzleManualWrite,
    PuzzlePrintPreview,
    PrintManifestRequest,
    PuzzleSummary,
    OIDCLoginSettingsWrite,
    OIDCProviderPatch,
    OIDCProviderWrite,
    PublicRegistrationCreate,
    PublicRegistrationResend,
    UserCreate,
    WordBankEntryWrite,
    WordBankPatch,
    WordBankWrite,
)
from app.oidc import (
    OIDCError,
    complete_authorization,
    decrypt_secret,
    encrypt_secret,
    new_authorization_request,
    normalize_issuer,
    normalize_scopes,
    provider_metadata,
    provider_summary,
)
from app.version import __version__


DEFAULT_APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8790").rstrip("/")
TRANSPORT_CONFIG = load_transport_config()
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/data/uploads"))
PHOTO_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
PHOTO_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-local-session-secret")
PUZZARY_TIMEZONE = os.getenv("PUZZARY_TIMEZONE", "America/New_York")
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="Puzzary",
    description="An extensible puzzle and game library with a flexible API and plugin-based game system.",
    version=__version__,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    max_age=60 * 60 * 24 * 7,
    https_only=TRANSPORT_CONFIG.secure_cookies,
)
if TRANSPORT_CONFIG.https_redirect:
    app.add_middleware(HTTPSRedirectMiddleware)


@app.middleware("http")
async def redact_legacy_password_setup_query(request: Request, call_next):
    path = request.scope.get("path", "")
    original_query = request.scope.get("query_string", b"")
    response = await call_next(request)
    if path in {"/password/setup", "/register/verify"} and b"token=" in original_query:
        request.scope["query_string"] = b"token=REDACTED"
    return response


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"), context_processors=[appearance_template_context])
templates.env.loader = ChoiceLoader([
    FileSystemLoader(str(path)) for path in plugin_template_directories()
] + [templates.env.loader])
templates.env.globals['can_owned'] = can_owned
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
for plugin_slug, static_dir in plugin_static_directories().items():
    app.mount(f"/plugins/{plugin_slug}/static", StaticFiles(directory=str(static_dir)), name=f"plugin-{plugin_slug}-static")

for endpoint_name in (
    "api_list_oidc_providers",
    "api_create_oidc_provider",
    "api_update_oidc_provider",
    "api_delete_oidc_provider",
    "api_update_oidc_settings",
    "api_test_oidc_provider",
):
    register_route_permission(endpoint_name, "settings.manage")
from app.publishing import router as publishing_router
app.include_router(publishing_router)
from app.feeds import router as feeds_router
app.include_router(feeds_router)
from app import notifications as puzzle_notifications
app.include_router(puzzle_notifications.router)
from app.groups import router as groups_router
app.include_router(groups_router)
from app.permission_admin import router as permission_router
app.include_router(permission_router)
from app.word_bank_admin import router as word_bank_router
app.include_router(word_bank_router)
for plugin in list_puzzle_types():
    if plugin.router is not None:
        app.include_router(plugin.router)

DICTIONARY_SYNC_CHECK_SECONDS = 15 * 60

PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 390000
RESET_TOKEN_TTL_HOURS = 24
LOGIN_RATE_LIMIT_WINDOW = 300
LOGIN_RATE_LIMIT_MAX = 8
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
REGISTRATION_RATE_LIMIT_WINDOW = 15 * 60
REGISTRATION_RATE_LIMIT_MAX = 5
REGISTRATION_IP_RATE_LIMIT_MAX = 20
REGISTRATION_RESEND_WINDOW = 60 * 60
REGISTRATION_RESEND_MAX = 5
REGISTRATION_RESEND_MIN_SECONDS = 60
REGISTRATION_RETENTION_DAYS = 7
REGISTRATION_ATTEMPTS: dict[str, deque[float]] = {}
REGISTRATION_ATTEMPT_LOCK = threading.Lock()
PUZZLE_PASSWORD_ATTEMPT_LIMIT = 5
PUZZLE_PASSWORD_ATTEMPT_WINDOW = 60
PUZZLE_PASSWORD_ATTEMPTS: dict[tuple[str, str], deque[float]] = {}
PUZZLE_PASSWORD_ATTEMPT_LOCK = threading.Lock()


def tag_value(tags: list[PuzzleTag], tag_type: str) -> str:
    for tag in tags:
        if tag.tag_type == tag_type:
            return tag.name
    return ""


def tag_values(tags: list[PuzzleTag], tag_type: str) -> list[str]:
    return [tag.name for tag in tags if tag.tag_type == tag_type]


def tag_lines(tags: list[PuzzleTag], tag_type: str) -> str:
    return "\n".join(tag_values(tags, tag_type))


templates.env.globals["tag_value"] = tag_value
templates.env.globals["tag_values"] = tag_values
templates.env.globals["tag_lines"] = tag_lines
templates.env.globals["definition_clue_lines"] = lambda definition: "\n".join(stored_definition_clues(definition))


def app_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(PUZZARY_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def local_datetime_value(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(app_timezone()).strftime("%Y-%m-%dT%H:%M")


def local_datetime_label(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(app_timezone()).strftime("%Y-%m-%d %I:%M %p %Z")


def default_auto_archive_at(publication_time: datetime | None = None) -> datetime:
    return (publication_time or utcnow()) + timedelta(hours=24)


def default_auto_archive_value() -> str:
    return local_datetime_value(default_auto_archive_at())


templates.env.globals["local_datetime_value"] = local_datetime_value
templates.env.globals["local_datetime_label"] = local_datetime_label
templates.env.globals["activity_datetime_label"] = lambda value: local_datetime_label(datetime.fromisoformat(value))
templates.env.globals["activity_event_summary"] = event_summary
templates.env.globals["default_auto_archive_value"] = default_auto_archive_value
templates.env.globals["puzzary_timezone"] = PUZZARY_TIMEZONE
templates.env.globals["puzzle_publication_state"] = puzzle_publication_state
templates.env.globals["puzzle_is_published"] = puzzle_is_published
templates.env.globals["puzzle_is_scheduled"] = puzzle_is_scheduled


def template_exists(template_name: str) -> bool:
    try:
        templates.env.get_template(template_name)
    except TemplateNotFound:
        return False
    return True


@app.on_event("startup")
async def startup() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        ensure_admin_user(db)
        initialize_permissions(db)
        seed_word_banks(db)
        seed_word_definitions(db)
        apply_due_auto_archives(db)
        puzzle_notifications.ensure_vapid_keys(db)
        puzzle_notifications.apply_due_publication_notifications(db)
        run_due_dictionary_import_sources(db)
        effective_base_url = app_base_url(db)
        effective_https = effective_base_url.lower().startswith("https://")
        if effective_https and not TRANSPORT_CONFIG.secure_cookies:
            logging.getLogger("puzzary.transport").warning(
                "The effective public base URL is HTTPS but session cookies are not Secure. "
                "Set APP_BASE_URL to the HTTPS origin or PUZZARY_SECURE_COOKIES=true, then restart Puzzary."
            )
        if (TRANSPORT_CONFIG.native_tls or TRANSPORT_CONFIG.https_redirect) and not effective_https:
            logging.getLogger("puzzary.transport").warning(
                "TLS or HTTPS redirect is enabled while the saved public base URL is HTTP. "
                "Update Admin > Settings to prevent mixed-content links."
            )
        for warning in TRANSPORT_CONFIG.warnings:
            logging.getLogger("puzzary.transport").warning(warning)
    app.state.auto_archive_task = asyncio.create_task(auto_archive_loop())
    app.state.dictionary_sync_task = asyncio.create_task(dictionary_import_source_loop())
    app.state.notification_task = asyncio.create_task(puzzle_notifications.notification_loop())
    app.state.registration_cleanup_task = asyncio.create_task(registration_cleanup_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    task = getattr(app.state, "auto_archive_task", None)
    if task is not None:
        task.cancel()
    sync_task = getattr(app.state, "dictionary_sync_task", None)
    if sync_task is not None:
        sync_task.cancel()
    notification_task = getattr(app.state, "notification_task", None)
    if notification_task is not None:
        notification_task.cancel()
    registration_cleanup_task = getattr(app.state, "registration_cleanup_task", None)
    if registration_cleanup_task is not None:
        registration_cleanup_task.cancel()


def normalize_base_url(value: str) -> str:
    text = value.strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Base URL must be an HTTP or HTTPS origin without credentials, path, query, or fragment.")
    return text


def app_setting(db: Session, key: str, default: str = "") -> str:
    setting = db.get(AppSetting, key)
    if setting is None or not setting.value:
        return default
    return setting.value


def set_app_setting(db: Session, key: str, value: str) -> None:
    setting = db.get(AppSetting, key)
    if setting is None:
        setting = AppSetting(key=key, value=value)
        db.add(setting)
    else:
        setting.value = value


def boolean_setting(db: Session, key: str, default: bool = False) -> bool:
    value = app_setting(db, key, "true" if default else "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


REGISTRATION_SETTING_KEYS = {
    "enabled": "public_registration_enabled",
    "verification_required": "public_registration_email_verification_required",
    "role": "public_registration_role_id",
    "expiry": "public_registration_verification_hours",
    "smtp_host": "registration_smtp_host",
    "smtp_port": "registration_smtp_port",
    "smtp_security": "registration_smtp_security",
    "smtp_username": "registration_smtp_username",
    "smtp_password": "registration_smtp_password_encrypted",
    "smtp_from": "registration_smtp_from",
}


def safe_registration_roles(db: Session) -> list[PermissionRole]:
    return [
        role
        for role in db.scalars(select(PermissionRole).order_by(PermissionRole.name, PermissionRole.id))
        if set(role.permissions or []) <= PLAYER
    ]


def registration_default_role(db: Session) -> PermissionRole:
    safe_roles = safe_registration_roles(db)
    selected = app_setting(db, REGISTRATION_SETTING_KEYS["role"])
    if selected.isdigit():
        role = next((item for item in safe_roles if item.id == int(selected)), None)
        if role is not None:
            return role
    player = next((item for item in safe_roles if item.name == "Player"), None)
    if player is None:
        initialize_permissions(db)
        safe_roles = safe_registration_roles(db)
        player = next((item for item in safe_roles if item.name == "Player"), None)
    if player is None:
        raise RuntimeError("The safe Player role is unavailable.")
    return player


def registration_verification_hours(db: Session) -> int:
    try:
        return max(1, min(168, int(app_setting(db, REGISTRATION_SETTING_KEYS["expiry"], "24"))))
    except ValueError:
        return 24


def registration_verification_required(db: Session) -> bool:
    return boolean_setting(db, REGISTRATION_SETTING_KEYS["verification_required"], False)


def registration_mail_settings(db: Session, include_password: bool = False) -> dict:
    password = ""
    encrypted = app_setting(db, REGISTRATION_SETTING_KEYS["smtp_password"])
    if include_password and encrypted:
        try:
            password = decrypt_secret(encrypted, SESSION_SECRET)
        except OIDCError as error:
            raise ValueError("The saved email password cannot be decrypted with this server key.") from error
    try:
        port = int(app_setting(db, REGISTRATION_SETTING_KEYS["smtp_port"], "587"))
    except ValueError:
        port = 587
    security = app_setting(db, REGISTRATION_SETTING_KEYS["smtp_security"], "starttls")
    if security not in {"starttls", "tls", "none"}:
        security = "starttls"
    return {
        "host": app_setting(db, REGISTRATION_SETTING_KEYS["smtp_host"]),
        "port": port,
        "security": security,
        "username": app_setting(db, REGISTRATION_SETTING_KEYS["smtp_username"]),
        "password": password,
        "password_configured": bool(encrypted),
        "from_address": app_setting(db, REGISTRATION_SETTING_KEYS["smtp_from"]),
    }


def registration_mail_ready(db: Session) -> bool:
    settings = registration_mail_settings(db)
    return bool(settings["host"] and settings["from_address"] and 1 <= settings["port"] <= 65535)


def registration_public_url_safe(db: Session) -> bool:
    parsed = urlparse(app_base_url(db))
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme == "https" or hostname in {"localhost", "127.0.0.1", "::1"}


def public_registration_enabled(db: Session) -> bool:
    return boolean_setting(db, REGISTRATION_SETTING_KEYS["enabled"], False)


def public_registration_available(db: Session) -> bool:
    return (
        public_registration_enabled(db)
        and oidc_login_mode(db) != "oidc"
        and (not registration_verification_required(db) or registration_mail_ready(db))
        and registration_public_url_safe(db)
    )


def registration_settings_view(db: Session) -> dict:
    mail = registration_mail_settings(db)
    selected_role = registration_default_role(db)
    return {
        "enabled": public_registration_enabled(db),
        "available": public_registration_available(db),
        "verification_required": registration_verification_required(db),
        "verification_hours": registration_verification_hours(db),
        "default_role_id": selected_role.id,
        "safe_roles": safe_registration_roles(db),
        "mail": mail,
    }


def save_registration_settings(
    db: Session,
    actor: User,
    *,
    enabled: bool,
    verification_required: bool,
    role_id: int,
    verification_hours: int,
    smtp_host: str,
    smtp_port: int,
    smtp_security: str,
    smtp_username: str,
    smtp_password: str,
    smtp_from: str,
    remove_smtp_password: bool,
    target_login_mode: str,
) -> None:
    safe_roles = safe_registration_roles(db)
    role = next((item for item in safe_roles if item.id == role_id), None)
    if role is None:
        raise ValueError("Choose a non-privileged default registration role.")
    if not 1 <= verification_hours <= 168:
        raise ValueError("Verification links must expire between 1 and 168 hours.")
    host = smtp_host.strip()
    if len(host) > 255 or any(character.isspace() for character in host) or any(character in host for character in "/@"):
        raise ValueError("SMTP host must be a hostname or IP address without a URL or credentials.")
    if not 1 <= smtp_port <= 65535:
        raise ValueError("SMTP port must be between 1 and 65535.")
    if smtp_security not in {"starttls", "tls", "none"}:
        raise ValueError("Unknown SMTP security mode.")
    username = smtp_username.strip()
    if len(username) > 255:
        raise ValueError("SMTP username is too long.")
    from_address = validate_mailbox(smtp_from, "From address") if smtp_from.strip() else ""
    if len(smtp_password) > 2000:
        raise ValueError("SMTP password is too long.")
    if smtp_password and remove_smtp_password:
        raise ValueError("Enter a new SMTP password or remove the saved one, not both.")
    password_configured = bool(app_setting(db, REGISTRATION_SETTING_KEYS["smtp_password"]))
    if remove_smtp_password:
        password_configured = False
    elif smtp_password:
        password_configured = True
    if username and not password_configured:
        raise ValueError("Save an SMTP password when an SMTP username is configured.")
    if enabled and target_login_mode == "oidc":
        raise ValueError("Public local-account registration cannot be enabled in single-sign-on-only mode.")
    if enabled and verification_required and (not host or not from_address):
        raise ValueError("Configure an SMTP host and From address before requiring email verification.")
    if enabled and not registration_public_url_safe(db):
        raise ValueError("Public registration requires an HTTPS Public Base URL (localhost is allowed for testing).")

    before = registration_settings_view(db)
    set_app_setting(db, REGISTRATION_SETTING_KEYS["enabled"], "true" if enabled else "false")
    set_app_setting(
        db,
        REGISTRATION_SETTING_KEYS["verification_required"],
        "true" if verification_required else "false",
    )
    set_app_setting(db, REGISTRATION_SETTING_KEYS["role"], str(role.id))
    set_app_setting(db, REGISTRATION_SETTING_KEYS["expiry"], str(verification_hours))
    set_app_setting(db, REGISTRATION_SETTING_KEYS["smtp_host"], host)
    set_app_setting(db, REGISTRATION_SETTING_KEYS["smtp_port"], str(smtp_port))
    set_app_setting(db, REGISTRATION_SETTING_KEYS["smtp_security"], smtp_security)
    set_app_setting(db, REGISTRATION_SETTING_KEYS["smtp_username"], username)
    set_app_setting(db, REGISTRATION_SETTING_KEYS["smtp_from"], from_address)
    if remove_smtp_password:
        setting = db.get(AppSetting, REGISTRATION_SETTING_KEYS["smtp_password"])
        if setting is not None:
            db.delete(setting)
    elif smtp_password:
        set_app_setting(
            db,
            REGISTRATION_SETTING_KEYS["smtp_password"],
            encrypt_secret(smtp_password, SESSION_SECRET),
        )
    audit(
        db,
        actor,
        "settings",
        "public_registration",
        "registration.settings_updated",
        {
            "enabled": before["enabled"],
            "verification_required": before["verification_required"],
            "role_id": before["default_role_id"],
            "verification_hours": before["verification_hours"],
            "mail_configured": bool(before["mail"]["host"] and before["mail"]["from_address"]),
        },
        {
            "enabled": enabled,
            "verification_required": verification_required,
            "role_id": role.id,
            "verification_hours": verification_hours,
            "mail_configured": bool(host and from_address),
        },
    )


def validate_mailbox(value: str, label: str = "Email address") -> str:
    address = unicodedata.normalize("NFKC", value.strip()).casefold()
    if (
        not address
        or len(address) > 255
        or address.count("@") != 1
        or any(character.isspace() for character in address)
    ):
        raise ValueError(f"{label} is not valid.")
    local, domain = address.rsplit("@", 1)
    if not local or not domain or domain.startswith(".") or domain.endswith(".") or "." not in domain:
        raise ValueError(f"{label} is not valid.")
    return address


def send_registration_verification(db: Session, user: User, token: str) -> None:
    settings = registration_mail_settings(db, include_password=True)
    if not settings["host"] or not settings["from_address"]:
        raise ValueError("Registration email delivery is not configured.")
    message = EmailMessage()
    message["Subject"] = "Verify your Puzzary account"
    message["From"] = settings["from_address"]
    message["To"] = user.email or ""
    verification_url = f"{app_base_url(db)}/register/verify?token={quote(token)}"
    message.set_content(
        f"Hello {user.display_name},\n\n"
        "Verify your Puzzary account by opening this one-time link:\n\n"
        f"{verification_url}\n\n"
        f"This link expires in {registration_verification_hours(db)} hours. "
        "If you did not request this account, ignore this message.\n"
    )
    context = ssl.create_default_context()
    if settings["security"] == "tls":
        client = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=10, context=context)
    else:
        client = smtplib.SMTP(settings["host"], settings["port"], timeout=10)
    with client:
        client.ehlo()
        if settings["security"] == "starttls":
            client.starttls(context=context)
            client.ehlo()
        if settings["username"]:
            client.login(settings["username"], settings["password"])
        client.send_message(message)


def cleanup_stale_registrations(db: Session) -> int:
    cutoff = utcnow() - timedelta(days=REGISTRATION_RETENTION_DAYS)
    users = list(
        db.scalars(
            select(User).where(
                User.registration_source == "self",
                User.is_active == False,  # noqa: E712
                User.email_verified_at.is_(None),
                User.registration_created_at.is_not(None),
                User.registration_created_at < cutoff,
            )
        )
    )
    for user in users:
        user.display_name = f"Expired registration {user.id}"
        user.display_name_normalized = f"expired-registration-{user.id}"
        user.email = None
        user.email_normalized = None
        user.password_hash = None
        user.email_verification_token_hash = None
        user.email_verification_expires_at = None
        user.email_verification_sent_at = None
        user.registration_source = "expired"
    if users:
        db.commit()
    return len(users)


async def registration_cleanup_loop() -> None:
    while True:
        with SessionLocal() as db:
            cleanup_stale_registrations(db)
        await asyncio.sleep(6 * 60 * 60)


PLUGIN_GITHUB_TOKEN_SETTING = "plugin_github_token_encrypted"


def stored_plugin_github_token(db: Session) -> str:
    encrypted = app_setting(db, PLUGIN_GITHUB_TOKEN_SETTING)
    if not encrypted:
        return ""
    try:
        return decrypt_secret(encrypted, SESSION_SECRET)
    except OIDCError as error:
        raise ValueError("The saved GitHub credential cannot be decrypted with this server key.") from error


def plugin_github_access(db: Session) -> dict:
    stored = bool(app_setting(db, PLUGIN_GITHUB_TOKEN_SETTING))
    environment = bool(os.getenv("PUZZARY_PLUGIN_GITHUB_TOKEN", "").strip())
    return {
        "configured": stored or environment,
        "source": "saved" if stored else "environment" if environment else "none",
        "stored": stored,
        "environment_fallback": environment,
    }


def plugin_github_token(db: Session) -> str:
    stored = stored_plugin_github_token(db)
    return stored or os.getenv("PUZZARY_PLUGIN_GITHUB_TOKEN", "").strip()


def validate_plugin_github_token(value: str) -> str:
    token = value.strip()
    if not token:
        raise ValueError("Enter a GitHub credential.")
    if len(token) > 2000 or any(character.isspace() for character in token):
        raise ValueError("The GitHub credential is not valid.")
    return token


def save_plugin_github_token(db: Session, user: User, value: str) -> None:
    token = validate_plugin_github_token(value)
    replacing = bool(app_setting(db, PLUGIN_GITHUB_TOKEN_SETTING))
    set_app_setting(db, PLUGIN_GITHUB_TOKEN_SETTING, encrypt_secret(token, SESSION_SECRET))
    audit(db, user, "plugin_github_access", "private", "plugin.github_access.saved", {"configured": replacing}, {"configured": True})
    db.commit()


def remove_plugin_github_token(db: Session, user: User) -> None:
    setting = db.get(AppSetting, PLUGIN_GITHUB_TOKEN_SETTING)
    existed = setting is not None
    if setting is not None:
        db.delete(setting)
    audit(db, user, "plugin_github_access", "private", "plugin.github_access.removed", {"configured": existed}, {"configured": False})
    db.commit()


def check_plugin_github_token(db: Session, user: User) -> dict:
    result = test_github_access(plugin_github_token(db))
    audit(db, user, "plugin_github_access", "private", "plugin.github_access.tested", {}, {
        "repository": result["repository"],
        "accessible": True,
    })
    db.commit()
    return result


def disabled_puzzle_type_slugs(db: Session) -> set[str]:
    raw_value = app_setting(db, "disabled_puzzle_types", "[]")
    try:
        values = json.loads(raw_value)
    except json.JSONDecodeError:
        values = []
    if not isinstance(values, list):
        return set()
    registered = {puzzle_type.slug for puzzle_type in list_puzzle_types()}
    return {str(value) for value in values if str(value) in registered}


def set_disabled_puzzle_type_slugs(db: Session, slugs: list[str]) -> None:
    registered = {puzzle_type.slug for puzzle_type in list_puzzle_types()}
    disabled = sorted({str(slug) for slug in slugs if str(slug) in registered})
    set_app_setting(db, "disabled_puzzle_types", json.dumps(disabled))


def is_puzzle_type_enabled(db: Session, slug: str) -> bool:
    return slug not in disabled_puzzle_type_slugs(db)


def list_enabled_puzzle_types(db: Session) -> list:
    disabled = disabled_puzzle_type_slugs(db)
    return [puzzle_type for puzzle_type in list_puzzle_types() if puzzle_type.slug not in disabled]


def app_base_url(db: Session | None = None) -> str:
    if db is None:
        return DEFAULT_APP_BASE_URL
    return app_setting(db, "public_base_url", DEFAULT_APP_BASE_URL).rstrip("/")


OIDC_LOGIN_MODES = {"local", "mixed", "oidc"}
OIDC_PROVISIONING_MODES = {"link_only", "jit"}
OIDC_ATTEMPT_TTL_MINUTES = 10


def oidc_login_mode(db: Session) -> str:
    mode = app_setting(db, "oidc_login_mode", "local")
    return mode if mode in OIDC_LOGIN_MODES else "local"


def oidc_callback_url(db: Session) -> str:
    return f"{app_base_url(db)}/auth/oidc/callback"


def oidc_enabled_providers(db: Session) -> list[OIDCProvider]:
    return list(
        db.scalars(
            select(OIDCProvider)
            .where(OIDCProvider.enabled == True)  # noqa: E712
            .order_by(OIDCProvider.display_name, OIDCProvider.id)
        )
    )


def restore_local_login_if_no_oidc_provider(db: Session) -> None:
    db.flush()
    if oidc_login_mode(db) != "local" and not oidc_enabled_providers(db):
        set_app_setting(db, "oidc_login_mode", "local")


def oidc_provider_payload(provider: OIDCProvider, db: Session) -> dict:
    return provider_summary(provider, oidc_callback_url(db))


def validate_oidc_provider_values(
    display_name: str,
    issuer_url: str,
    client_id: str,
    scopes: str,
    provisioning_mode: str,
) -> tuple[str, str, str, str, str]:
    name = display_name.strip()
    client = client_id.strip()
    if not name:
        raise OIDCError("Provider display name is required.")
    if not client:
        raise OIDCError("OIDC client ID is required.")
    provisioning = provisioning_mode.strip().lower()
    if provisioning not in OIDC_PROVISIONING_MODES:
        raise OIDCError("Provisioning mode must be explicit linking or player-only automatic creation.")
    return name[:120], normalize_issuer(issuer_url), client[:255], normalize_scopes(scopes), provisioning


def update_oidc_provider(
    provider: OIDCProvider,
    *,
    display_name: str,
    issuer_url: str,
    client_id: str,
    client_secret: str | None,
    scopes: str,
    provisioning_mode: str,
    enabled: bool,
) -> None:
    name, issuer, client, clean_scopes, provisioning = validate_oidc_provider_values(
        display_name, issuer_url, client_id, scopes, provisioning_mode
    )
    changed_protocol = (
        provider.issuer_url != issuer
        or provider.client_id != client
        or bool(client_secret)
        or provider.scopes != clean_scopes
    )
    provider.display_name = name
    provider.issuer_url = issuer
    provider.client_id = client
    provider.scopes = clean_scopes
    provider.provisioning_mode = provisioning
    if client_secret:
        provider.client_secret_encrypted = encrypt_secret(client_secret, SESSION_SECRET)
    if not provider.client_secret_encrypted:
        raise OIDCError("OIDC client secret is required.")
    if changed_protocol:
        provider.last_tested_at = None
        provider.last_test_status = ""
        provider.last_test_message = "Configuration changed; test it again before enabling."
    if enabled and not (provider.last_tested_at and provider.last_test_status == "ok"):
        raise OIDCError("Test this provider successfully before enabling it.")
    provider.enabled = enabled


def unique_oidc_display_name(db: Session, value: str) -> str:
    base = re.sub(r"\s+", " ", value).strip()[:70] or "OIDC user"
    candidate = base
    number = 2
    while db.scalar(select(User.id).where(
        (User.display_name == candidate)
        | (User.display_name_normalized == normalized_identity(candidate))
    )) is not None:
        suffix = f" {number}"
        candidate = f"{base[:80 - len(suffix)]}{suffix}"
        number += 1
    return candidate


def puzzle_url(puzzle_id: str, db: Session | None = None) -> str:
    return f"{app_base_url(db)}/puzzles/{puzzle_id}"


def puzzle_print_url(puzzle_id: str, db: Session | None = None) -> str:
    return f"{app_base_url(db)}/puzzles/{puzzle_id}/print"


def puzzle_print_render_url(puzzle_id: str, db: Session | None = None) -> str:
    return f"{app_base_url(db)}/puzzles/{puzzle_id}/print/render"


def authorized_render_url(puzzle, user, request, db):
    token = URLSafeTimedSerializer(SESSION_SECRET, salt='puzzary-print-render').dumps({
        'puzzle_id':puzzle.id, 'user_id':user.id, 'session_version':user.session_version,
        'api':bool(request.headers.get('authorization')),
        'puzzle_access': puzzle_unlock_fingerprint(puzzle) if puzzle.access_password_hash else '',
    })
    return puzzle_print_render_url(puzzle.id,db) + '?' + urlencode({'render_token':token})


def puzzle_qr_short_url(puzzle_id: str, db: Session | None = None) -> str:
    return f"{app_base_url(db)}/q/{puzzle_id}"


def puzzle_qr_svg_url(puzzle_id: str, db: Session | None = None) -> str:
    return f"{app_base_url(db)}/puzzles/{puzzle_id}/qr.svg"


PRINT_PAGE_PRESETS = {
    "receipt": {"label": "Receipt roll", "width_mm": 80.0, "height_mm": None, "margin_mm": 4.0},
    "zebra": {"label": "Zebra roll", "width_mm": 80.0, "height_mm": None, "margin_mm": 4.0},
    "4x6": {"label": "4 x 6 in", "width_mm": 101.6, "height_mm": 152.4, "margin_mm": 5.0},
    "letter": {"label": "US Letter", "width_mm": 215.9, "height_mm": 279.4, "margin_mm": 8.0},
    "a4": {"label": "A4", "width_mm": 210.0, "height_mm": 297.0, "margin_mm": 8.0},
}


def positive_float(value: str | None, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number <= 0:
        return default
    return number


def apply_print_request_layout(layout: dict, request: Request | None = None) -> dict:
    params = request.query_params if request is not None else {}
    requested_size = str(
        params.get("page_size") or params.get("size") or layout.get("page_size") or "receipt"
    ).strip().lower()
    if requested_size not in PRINT_PAGE_PRESETS and requested_size != "custom":
        requested_size = "receipt"

    preset = PRINT_PAGE_PRESETS.get(requested_size, PRINT_PAGE_PRESETS["receipt"])
    width_mm = positive_float(params.get("width_mm"), preset["width_mm"])
    height_mm = positive_float(params.get("height_mm"), preset["height_mm"])
    margin_mm = positive_float(params.get("margin_mm"), preset["margin_mm"]) or 0
    if width_mm is None:
        width_mm = PRINT_PAGE_PRESETS["receipt"]["width_mm"]

    if not 25 <= width_mm <= 1000 or (height_mm is not None and not 25 <= height_mm <= 5000):
        raise HTTPException(status_code=422, detail="Width must be 25-1000mm and height 25-5000mm")
    if params.get("margin_mm") == "0":
        margin_mm = 0

    shortest_side = min(width_mm, height_mm) if height_mm is not None else width_mm
    margin_mm = min(margin_mm, max((shortest_side - 10) / 2, 0))
    content_width_mm = max(width_mm - (margin_mm * 2), 10)

    layout["page_size"] = requested_size
    layout["page_size_label"] = "Custom" if requested_size == "custom" else preset["label"]
    layout["page_width_mm"] = round(width_mm, 2)
    layout["page_height_mm"] = round(height_mm, 2) if height_mm else None
    layout["page_margin_mm"] = round(margin_mm, 2)
    layout["content_width_mm"] = round(content_width_mm, 2)
    layout["page_size_presets"] = [
        {"value": key, "label": value["label"]} for key, value in PRINT_PAGE_PRESETS.items()
    ] + [{"value": "custom", "label": "Custom"}]
    return layout


def qr_png_data_uri(value: str) -> str:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2, box_size=8)
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(image_factory=PyPNGImage)
    buffer = BytesIO()
    image.save(buffer)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def print_layout_context(puzzle: Puzzle, puzzle_type, db: Session | None = None) -> dict:
    layout = puzzle_type.print_layout.as_dict()
    if puzzle_type.print_layout_builder is not None:
        try:
            override = puzzle_type.print_layout_builder(
                puzzle.print_payload or {},
                puzzle.play_payload or {},
                puzzle.puzzle_metadata or {},
            )
        except Exception:
            override = {}
        if isinstance(override, dict):
            layout.update(override)
    layout.setdefault("slug", puzzle.puzzle_type)
    layout.setdefault("sheet_class", "print-layout-default")
    layout.setdefault("page_size", "receipt")
    layout.setdefault("fit_to_width", True)
    layout.setdefault("include_open_link", True)
    layout.setdefault("include_solution", False)
    layout.setdefault("show_prompt", True)
    layout.setdefault("instructions", "")
    layout.setdefault("notes", [])
    layout.setdefault("max_width_px", 760)
    layout["url"] = puzzle_url(puzzle.id, db)
    layout["print_url"] = puzzle_print_url(puzzle.id, db)
    layout["render_url"] = puzzle_print_render_url(puzzle.id, db)
    layout["short_url"] = puzzle_qr_short_url(puzzle.id, db)
    layout["qr_url"] = puzzle_qr_svg_url(puzzle.id, db)
    layout["qr_target_url"] = puzzle_qr_short_url(puzzle.id, db)
    layout["qr_data_uri"] = qr_png_data_uri(layout["qr_target_url"])
    return layout


def serialize_puzzle(puzzle: Puzzle, db: Session | None = None) -> PuzzleSummary:
    plugin = get_puzzle_type(puzzle.puzzle_type)
    private_keys = plugin.private_metadata_keys if plugin else ()
    return PuzzleSummary(
        id=puzzle.id,
        puzzle_type=puzzle.puzzle_type,
        title=puzzle.title,
        theme=puzzle.theme,
        difficulty=puzzle.difficulty,
        completion_message=puzzle.completion_message or "",
        url=puzzle_url(puzzle.id, db),
        print_payload=puzzle.print_payload,
        play_payload=public_play_payload(puzzle),
        metadata={key: value for key, value in (puzzle.puzzle_metadata or {}).items() if key not in private_keys},
        hints=[hint.text for hint in sorted(puzzle.hints, key=lambda item: item.order)],
        visibility="unlisted" if puzzle_is_unlisted(puzzle) else "public",
        password_protected=bool(puzzle.access_password_hash),
        publication_state=puzzle_publication_state(puzzle),
        publish_at=puzzle.publish_at,
        offline_allowed=not bool((puzzle.puzzle_metadata or {}).get("offline_disabled")),
    )


def public_play_payload(puzzle: Puzzle) -> dict:
    """Return only data that is safe to expose to an ordinary player."""
    payload = deepcopy(puzzle.play_payload or {})
    if puzzle.puzzle_type == "word_search":
        solution_gimmes = (puzzle.solution_payload or {}).get("gimmes", {})
        words = solution_gimmes.get("words", []) if isinstance(solution_gimmes, dict) else []
        payload["gimmes"] = {
            "mode": "found_words",
            "words": [str(word).upper() for word in words if str(word).strip()],
            "cells": printable_gimme_cells(puzzle),
        }
    elif puzzle.puzzle_type == "crossword":
        payload["grid"] = [
            [
                {"open": bool(cell.get("open", cell.get("letter"))), "number": cell.get("number", "")}
                for cell in row
            ]
            for row in payload.get("grid", [])
        ]
        payload["clues"] = [
            {key: value for key, value in clue.items() if key != "answer"}
            for clue in payload.get("clues", [])
        ]
        payload["gimmes"] = deepcopy((puzzle.solution_payload or {}).get("gimmes", {}))
    return payload


def portable_puzzle_item(puzzle: Puzzle, db: Session | None = None) -> dict:
    return {
        "schema": "puzzary.puzzle.item.v1",
        "origin": {
            "id": puzzle.id,
            "url": puzzle_url(puzzle.id, db),
            "exported_from": app_base_url(db),
        },
        "puzzle_type": puzzle.puzzle_type,
        "title": puzzle.title,
        "theme": puzzle.theme,
        "difficulty": puzzle.difficulty,
        "prompt": puzzle.prompt,
        "completion_message": puzzle.completion_message or "",
        "print_payload": puzzle.print_payload or {},
        "play_payload": puzzle.play_payload or {},
        "solution_payload": puzzle.solution_payload or {},
        "metadata": puzzle.puzzle_metadata or {},
        "hints": [hint.text for hint in sorted(puzzle.hints, key=lambda item: item.order)],
        "category": puzzle.category.name if puzzle.category else "",
        "tags": [tag.label for tag in puzzle.tags],
        "source": puzzle.source,
        "created_at": puzzle.created_at.isoformat() if puzzle.created_at else None,
        "attribution": {
            "creator": creator_label(puzzle),
            "license": (puzzle.puzzle_metadata or {}).get("license", ""),
        },
    }


def portable_puzzle_bundle(puzzles: list[Puzzle], db: Session | None = None) -> dict:
    return {
        "schema": "puzzary.puzzle.bundle.v1",
        "exported_at": utcnow().isoformat(),
        "exported_from": app_base_url(db),
        "count": len(puzzles),
        "items": [portable_puzzle_item(puzzle, db) for puzzle in puzzles],
    }


def puzzle_import_sources_from_github(url: str, api_token: str = "") -> list[dict[str, str]]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host == "gist.github.com":
        gist_id = parsed.path.strip("/").split("/")[-1]
        if not gist_id:
            raise ValueError("GitHub Gist URL is missing a gist id")
        gist_api_url = f"https://api.github.com/gists/{gist_id}"
        gist_payload = json.loads(fetch_dictionary_source(gist_api_url, "generic", api_token)[0]["text"])
        sources = []
        for file_data in gist_payload.get("files", {}).values():
            raw_url = file_data.get("raw_url")
            filename = str(file_data.get("filename", "")).lower()
            if raw_url and filename.endswith((".json", ".puzzary")):
                sources.extend(fetch_dictionary_source(raw_url, "generic", api_token))
        if not sources:
            raise ValueError("No JSON puzzle files were found in that Gist")
        return sources
    sources = fetch_dictionary_source(url, "github", api_token)
    if len(sources) > 1:
        sources = [
            source for source in sources
            if str(source.get("url", "")).lower().endswith((".json", ".puzzary"))
        ]
    if not sources:
        raise ValueError("No JSON puzzle files were found in that GitHub source")
    return sources


def fetch_puzzle_import_sources(source_url: str, api_token: str = "") -> list[dict[str, str]]:
    url = source_url.strip()
    if not url:
        return []
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Puzzle import source must be an http or https URL")
    if parsed.netloc.lower() == "pastebin.com" or parsed.netloc.lower().endswith("github.com"):
        if "github.com" in parsed.netloc.lower():
            return puzzle_import_sources_from_github(url, api_token)
        return fetch_dictionary_source(url, "generic", api_token)
    if parsed.path.rstrip("/").endswith("/api/puzzles/export"):
        return fetch_dictionary_source(url, "generic", api_token)
    if re.search(r"/api/puzzles/[^/]+/export$", parsed.path.rstrip("/")):
        return fetch_dictionary_source(url, "generic", api_token)
    if parsed.path.rstrip("/") and parsed.path.rstrip("/") != "/":
        return fetch_dictionary_source(url, "generic", api_token)
    export_url = f"{parsed.scheme}://{parsed.netloc}/api/puzzles/export"
    return fetch_dictionary_source(export_url, "generic", api_token)


def puzzle_items_from_manifest(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("Puzzle import must be a JSON object")
    schema = str(payload.get("schema", ""))
    if schema == "puzzary.puzzle.bundle.v1":
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise ValueError("Puzzle bundle items must be a list")
        return [item for item in items if isinstance(item, dict)]
    if schema == "puzzary.puzzle.item.v1" or {"puzzle_type", "print_payload", "play_payload", "solution_payload"} <= set(payload.keys()):
        return [payload]
    if "items" in payload and isinstance(payload["items"], list):
        return [item for item in payload["items"] if isinstance(item, dict)]
    raise ValueError("Unsupported puzzle import schema")


def parse_puzzle_import_items(source_text: str = "", source_url: str = "", api_token: str = "") -> list[dict]:
    sources: list[dict[str, str]] = []
    if source_text.strip():
        sources.append({"url": "pasted-content", "content_type": "application/json", "text": source_text})
    sources.extend(fetch_puzzle_import_sources(source_url, api_token))
    if not sources:
        raise ValueError("Provide pasted JSON, an uploaded file, or a source URL")
    items: list[dict] = []
    for source in sources:
        try:
            payload = json.loads(source["text"])
        except json.JSONDecodeError as error:
            raise ValueError(f"Puzzle source is not valid JSON: {error}") from error
        source_items = puzzle_items_from_manifest(payload)
        for item in source_items:
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            item["metadata"] = {**metadata, "import_source_url": source.get("url", source_url)}
            items.append(item)
    return items


def normalize_import_id(item: dict) -> str:
    origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
    return str(origin.get("id") or item.get("id") or "").strip()


def existing_imported_puzzle(db: Session, item: dict) -> Puzzle | None:
    origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
    origin_id = normalize_import_id(item)
    origin_url = str(origin.get("url") or "").strip()
    imported = db.scalars(select(Puzzle).where(Puzzle.source == "imported")).all()
    for puzzle in imported:
        metadata = puzzle.puzzle_metadata if isinstance(puzzle.puzzle_metadata, dict) else {}
        imported_origin = metadata.get("import_origin") if isinstance(metadata.get("import_origin"), dict) else {}
        if origin_id and str(imported_origin.get("id") or "") == origin_id:
            return puzzle
        if origin_url and str(imported_origin.get("url") or "") == origin_url:
            return puzzle
    return None


def validate_portable_puzzle_item(db: Session, item: dict) -> tuple[bool, str]:
    puzzle_type_slug = str(item.get("puzzle_type") or "").strip()
    if not puzzle_type_slug:
        return False, "Missing puzzle_type"
    if get_puzzle_type(puzzle_type_slug) is None:
        return False, f"Puzzle type '{puzzle_type_slug}' is not installed"
    if not is_puzzle_type_enabled(db, puzzle_type_slug):
        return False, f"Puzzle type '{puzzle_type_slug}' is disabled"
    for key in ("print_payload", "play_payload", "solution_payload"):
        if not isinstance(item.get(key), dict):
            return False, f"{key} must be an object"
    return True, ""


def validate_manual_puzzle_payload(db: Session, payload: PuzzleManualWrite, allow_draft: bool = True) -> tuple[bool, str]:
    item = {
        "puzzle_type": payload.puzzle_type,
        "print_payload": payload.print_payload,
        "play_payload": payload.play_payload,
        "solution_payload": payload.solution_payload,
    }
    valid, error = validate_portable_puzzle_item(db, item)
    if not valid:
        return False, error
    if payload.draft and allow_draft:
        return True, ""
    for key in ("print_payload", "play_payload", "solution_payload"):
        value = getattr(payload, key)
        if not value:
            return False, f"{key} is required before publishing"
        kind = str(value.get("kind") or "").strip()
        if not kind:
            return False, f"{key}.kind is required before publishing"
        if kind != payload.puzzle_type:
            return False, f"{key}.kind must match puzzle_type"
    return True, ""


def parse_json_object_field(value: str, field_name: str) -> dict:
    text = value.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{field_name} must be valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def normalize_completion_message(value: str) -> str:
    message = value.strip()
    if len(message) > 500:
        raise ValueError("Completion message must be 500 characters or fewer")
    if any(ord(character) < 32 and character not in "\r\n\t" for character in message):
        raise ValueError("Completion message contains unsupported control characters")
    return message


def manual_payload_from_form(
    puzzle_type: str,
    title: str,
    theme: str,
    difficulty: str,
    completion_message: str,
    prompt: str,
    print_payload: str,
    play_payload: str,
    solution_payload: str,
    metadata: str,
    hints: str,
    draft: str,
    visibility: str = "public",
    access_password: str = "",
    remove_access_password: bool = False,
    publish_at: str = "",
    offline_allowed: bool = True,
) -> PuzzleManualWrite:
    hint_lines = [line.strip() for line in hints.splitlines() if line.strip()]
    if not title.strip():
        raise ValueError("Title is required for raw payload mode")
    return PuzzleManualWrite(
        puzzle_type=puzzle_type.strip(),
        title=title.strip(),
        theme=(theme.strip() or "manual")[:80],
        difficulty=(difficulty.strip() or "manual")[:40],
        completion_message=normalize_completion_message(completion_message),
        prompt=prompt.strip(),
        print_payload=parse_json_object_field(print_payload, "Print payload"),
        play_payload=parse_json_object_field(play_payload, "Play payload"),
        solution_payload=parse_json_object_field(solution_payload, "Solution payload"),
        metadata=parse_json_object_field(metadata, "Metadata"),
        hints=hint_lines,
        draft=draft == "true",
        visibility="unlisted" if visibility == "unlisted" else "public",
        access_password=access_password,
        remove_access_password=remove_access_password,
        publish_at=parse_local_datetime(publish_at, "Publication date/time") if publish_at.strip() else None,
        offline_allowed=offline_allowed,
    )


def write_manual_puzzle(
    db: Session,
    payload: PuzzleManualWrite,
    creator: User | None,
    puzzle: Puzzle | None = None,
) -> Puzzle:
    if creator:
        require_owned(creator, 'puzzles', 'edit', puzzle.created_by_id) if puzzle else require_permission(creator, 'puzzles.create')
        if not payload.draft:
            require_owned(creator, 'puzzles', 'publish', puzzle.created_by_id if puzzle else creator.id)
        if not payload.draft and payload.publish_at is not None:
            require_owned(creator, 'puzzles', 'schedule', puzzle.created_by_id if puzzle else creator.id)
    valid, error = validate_manual_puzzle_payload(db, payload)
    if not valid:
        raise ValueError(error)
    schedule_at = require_future_publish_at(payload.publish_at) if not payload.draft and payload.publish_at else None
    metadata = dict(payload.metadata or {})
    metadata["manual"] = True
    metadata["draft"] = bool(payload.draft)
    metadata["visibility"] = payload.visibility
    metadata["offline_disabled"] = not payload.offline_allowed
    metadata["manual_updated_at"] = utcnow().isoformat()
    now = datetime.now(timezone.utc)
    creating = puzzle is None
    before_state = None if creating else puzzle_publication_state(puzzle)
    before_publish_at = None if creating or puzzle.publish_at is None else puzzle.publish_at.isoformat()
    if creating:
        puzzle = Puzzle(
            id=uuid.uuid4().hex[:12],
            puzzle_type=payload.puzzle_type,
            title=payload.title[:160],
            theme=payload.theme[:80],
            difficulty=payload.difficulty[:40],
            prompt=payload.prompt,
            completion_message=normalize_completion_message(payload.completion_message),
            print_payload=payload.print_payload,
            play_payload=payload.play_payload,
            solution_payload=payload.solution_payload,
            puzzle_metadata=metadata,
            source="manual",
            publish_at=schedule_at,
            auto_archive_at=None if payload.draft else default_auto_archive_at(schedule_at),
            archived_at=now if payload.draft else None,
            created_by_id=creator.id if creator is not None else None,
        )
        db.add(puzzle)
        db.flush()
    else:
        puzzle.puzzle_type = payload.puzzle_type
        puzzle.title = payload.title[:160]
        puzzle.theme = payload.theme[:80]
        puzzle.difficulty = payload.difficulty[:40]
        puzzle.prompt = payload.prompt
        puzzle.completion_message = normalize_completion_message(payload.completion_message)
        puzzle.print_payload = payload.print_payload
        puzzle.play_payload = payload.play_payload
        puzzle.solution_payload = payload.solution_payload
        puzzle.puzzle_metadata = metadata
        puzzle.source = "manual"
        puzzle.publish_at = schedule_at
        puzzle.archived_at = now if payload.draft else None
        if payload.draft:
            puzzle.auto_archive_at = None
        elif puzzle.auto_archive_at is None or (
            schedule_at is not None
            and (puzzle.auto_archive_at.replace(tzinfo=timezone.utc) if puzzle.auto_archive_at.tzinfo is None else puzzle.auto_archive_at) <= schedule_at
        ):
            puzzle.auto_archive_at = default_auto_archive_at(schedule_at)
    update_puzzle_access_password(
        puzzle,
        payload.access_password,
        payload.remove_access_password if not creating else False,
    )
    puzzle.hints.clear()
    db.flush()
    for index, hint_text in enumerate(payload.hints, start=1):
        db.add(PuzzleHint(puzzle_id=puzzle.id, order=index, text=hint_text))
    after_state = puzzle_publication_state(puzzle)
    if creator is not None:
        action = None
        if after_state == "scheduled":
            action = "puzzle.rescheduled" if before_state == "scheduled" else "puzzle.scheduled"
        elif before_state == "scheduled" and after_state == "draft":
            action = "puzzle.schedule_canceled"
        elif after_state == "published" and (creating or before_state in {"draft", "scheduled"}):
            action = "puzzle.published"
        if action:
            audit(db, creator, "puzzle", puzzle.id, action, {
                "state": before_state, "publish_at": before_publish_at,
            }, {
                "state": after_state,
                "publish_at": puzzle.publish_at.isoformat() if puzzle.publish_at else None,
            })
    if after_state == "published" and (creating or before_state in {"draft", "scheduled"}):
        puzzle_notifications.queue_puzzle_publication(db, puzzle)
    db.commit()
    db.refresh(puzzle)
    return puzzle


def puzzle_is_draft(puzzle: Puzzle) -> bool:
    metadata = puzzle.puzzle_metadata if isinstance(puzzle.puzzle_metadata, dict) else {}
    return bool(metadata.get("draft"))


def manual_puzzle_publish_error(db: Session, puzzle: Puzzle) -> str:
    metadata = puzzle.puzzle_metadata if isinstance(puzzle.puzzle_metadata, dict) else {}
    if not (metadata.get("manual") or puzzle.source == "manual"):
        return ""
    payload = PuzzleManualWrite(
        puzzle_type=puzzle.puzzle_type,
        title=puzzle.title,
        theme=puzzle.theme,
        difficulty=puzzle.difficulty,
        prompt=puzzle.prompt,
        completion_message=puzzle.completion_message or "",
        print_payload=puzzle.print_payload or {},
        play_payload=puzzle.play_payload or {},
        solution_payload=puzzle.solution_payload or {},
        metadata=metadata,
        hints=[hint.text for hint in sorted(puzzle.hints, key=lambda item: item.order)],
        draft=False,
        visibility="unlisted" if puzzle_is_unlisted(puzzle) else "public",
    )
    valid, error = validate_manual_puzzle_payload(db, payload, allow_draft=False)
    return "" if valid else error


def active_word_definitions(word: DictionaryWord) -> list[DictionaryDefinition]:
    return [definition for definition in word.definitions if definition.archived_at is None]


templates.env.globals["active_word_definitions"] = active_word_definitions


def dashboard_review_context(
    db: Session,
    puzzles: list[Puzzle],
    dictionary_words: list[DictionaryWord],
    all_word_banks: list[WordBank],
    tags: list[PuzzleTag],
    bank_counts: dict | None = None,
    tag_usage: dict | None = None,
) -> dict:
    now = utcnow()
    soon_cutoff = now + timedelta(hours=24)
    draft_puzzles = [puzzle for puzzle in puzzles if puzzle_is_draft(puzzle)]
    incomplete_puzzles = []
    for puzzle in puzzles:
        error = manual_puzzle_publish_error(db, puzzle)
        if error:
            incomplete_puzzles.append({"puzzle": puzzle, "error": error})
    banned_words = [word for word in dictionary_words if word.banned_at is not None]
    archived_words = [word for word in dictionary_words if word.archived_at is not None]
    words_without_definitions = [
        word for word in dictionary_words
        if word.archived_at is None and word.banned_at is None and not active_word_definitions(word)
    ]
    definitions_without_clues = [
        definition
        for word in dictionary_words
        if word.archived_at is None and word.banned_at is None
        for definition in active_word_definitions(word)
        if not stored_definition_clues(definition)
    ]
    archived_banks = [bank for bank in all_word_banks if bank.archived_at is not None]
    empty_banks = [
        bank for bank in all_word_banks
        if bank.archived_at is None and (bank_counts[bank.id] if bank_counts is not None else len(word_bank_entries(db, bank))) == 0
    ]
    usage = tag_usage if tag_usage is not None else tag_usage_counts(db, tags)
    unused_tags = [
        tag for tag in tags
        if usage[tag.id]["total"] == 0
    ]
    auto_archive_soon = [
        puzzle for puzzle in puzzles
        if puzzle.archived_at is None and puzzle.auto_archive_at is not None and now <= puzzle.auto_archive_at <= soon_cutoff
    ]
    return {
        "counts": {
            "total_puzzles": len(puzzles),
            "draft_puzzles": len(draft_puzzles),
            "incomplete_puzzles": len(incomplete_puzzles),
            "total_words": len(dictionary_words),
            "banned_words": len(banned_words),
            "archived_words": len(archived_words),
            "words_without_definitions": len(words_without_definitions),
            "definitions_without_clues": len(definitions_without_clues),
            "archived_banks": len(archived_banks),
            "empty_banks": len(empty_banks),
            "unused_tags": len(unused_tags),
            "auto_archive_soon": len(auto_archive_soon),
        },
        "draft_puzzles": draft_puzzles[:8],
        "incomplete_puzzles": incomplete_puzzles[:8],
        "banned_words": banned_words[:8],
        "archived_words": archived_words[:8],
        "words_without_definitions": words_without_definitions[:8],
        "definitions_without_clues": definitions_without_clues[:8],
        "archived_banks": archived_banks[:8],
        "empty_banks": empty_banks[:8],
        "unused_tags": unused_tags[:8],
        "auto_archive_soon": auto_archive_soon[:8],
    }


def import_portable_puzzles(
    db: Session,
    items: list[dict],
    creator: User | None = None,
    dry_run: bool = True,
    duplicate_policy: str = "skip",
    import_ids: list[str] | None = None,
) -> dict:
    selected_ids = {str(item_id) for item_id in (import_ids or []) if str(item_id).strip()}
    result = {
        "dry_run": dry_run,
        "total": len(items),
        "processed": 0,
        "imported": 0,
        "skipped": 0,
        "duplicates": 0,
        "unsupported": 0,
        "items": [],
        "errors": [],
    }
    for index, item in enumerate(items, start=1):
        origin_id = normalize_import_id(item) or f"item-{index}"
        if selected_ids and origin_id not in selected_ids:
            continue
        result["processed"] += 1
        valid, error = validate_portable_puzzle_item(db, item)
        row = {
            "origin_id": origin_id,
            "title": str(item.get("title") or "Imported Puzzle"),
            "puzzle_type": str(item.get("puzzle_type") or ""),
            "action": "preview",
            "error": error,
        }
        if not valid:
            result["unsupported"] += 1
            result["skipped"] += 1
            row["action"] = "unsupported"
            result["items"].append(row)
            continue
        duplicate = existing_imported_puzzle(db, item)
        if duplicate is not None and duplicate_policy != "copy":
            result["duplicates"] += 1
            result["skipped"] += 1
            row["action"] = "duplicate"
            row["existing_id"] = duplicate.id
            result["items"].append(row)
            continue
        row["action"] = "import" if not dry_run else "ready"
        result["items"].append(row)
        if dry_run:
            continue
        origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        metadata = {
            **metadata,
            "imported_at": utcnow().isoformat(),
            "import_origin": origin,
            "import_schema": str(item.get("schema") or "puzzary.puzzle.item.v1"),
        }
        draft = bool(creator and not can_owned(creator,'puzzles','publish',creator.id))
        metadata['draft'] = draft
        puzzle = Puzzle(
            id=uuid.uuid4().hex[:12],
            puzzle_type=str(item.get("puzzle_type")),
            title=str(item.get("title") or "Imported Puzzle")[:160],
            theme=str(item.get("theme") or "imported")[:80],
            difficulty=str(item.get("difficulty") or "imported")[:40],
            prompt=str(item.get("prompt") or ""),
            completion_message=normalize_completion_message(str(item.get("completion_message") or "")),
            print_payload=item.get("print_payload") or {},
            play_payload=item.get("play_payload") or {},
            solution_payload=item.get("solution_payload") or {},
            puzzle_metadata=metadata,
            source="imported",
            auto_archive_at=None if draft else default_auto_archive_at(),
            archived_at=utcnow() if draft else None,
            created_by_id=creator.id if creator is not None else None,
        )
        db.add(puzzle)
        db.flush()
        for hint_index, hint_text in enumerate(item.get("hints") if isinstance(item.get("hints"), list) else [], start=1):
            if str(hint_text).strip():
                db.add(PuzzleHint(puzzle_id=puzzle.id, order=hint_index, text=str(hint_text).strip()))
        if not draft:
            puzzle_notifications.queue_puzzle_publication(db, puzzle)
        result["imported"] += 1
        row["local_id"] = puzzle.id
    if not dry_run:
        db.commit()
    return result


def puzzle_preview(puzzle: Puzzle) -> dict:
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    label = puzzle_type.label if puzzle_type is not None else puzzle.puzzle_type.replace("_", " ").title()
    if puzzle_type is None or puzzle_type.preview_builder is None:
        return fallback_preview(puzzle.puzzle_type, label)
    try:
        preview = puzzle_type.preview_builder(puzzle.print_payload or {}, puzzle.play_payload or {}, puzzle.puzzle_metadata or {})
    except Exception:
        return fallback_preview(puzzle.puzzle_type, label)
    if not isinstance(preview, dict) or not preview.get("kind"):
        return fallback_preview(puzzle.puzzle_type, label)
    preview.setdefault("label", label)
    preview.setdefault("meta", puzzle.puzzle_type.replace("_", " "))
    return preview


def parse_date_boundary(value: str, end_of_day: bool = False) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        if "T" in text:
            parsed = datetime.fromisoformat(text)
        else:
            parsed = datetime.fromisoformat(f"{text}T{'23:59:59' if end_of_day else '00:00:00'}")
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Date filter is not valid") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=app_timezone())
    return parsed.astimezone(timezone.utc)


def progress_map_for_user(db: Session, user: User, puzzles: list[Puzzle]) -> dict:
    return progress_states(db, user, puzzles)


def library_filter_state(request: Request, current_user: User) -> dict:
    params = request.query_params
    status = params.get("status", "any")
    if status not in {"any", "not_started", "in_progress", "completed"}:
        status = "any"
    completed = params.get("completed", "any")
    if completed not in {"any", "completed", "not_completed"}:
        completed = "any"
    started = params.get("started", "any")
    if started not in {"any", "started", "not_started"}:
        started = "any"
    archive_status = params.get("archive_status", "any")
    if archive_status not in {"any", "soon", "scheduled", "none"}:
        archive_status = "any"
    availability = params.get("availability", "active")
    if availability not in {"active", "archived", "scheduled", "any"}:
        availability = "active"
    sort = params.get("sort", "created_desc")
    valid_sorts = {
        "created_desc",
        "created_asc",
        "archive_asc",
        "archive_desc",
        "title_asc",
        "title_desc",
        "type_asc",
        "difficulty_asc",
        "creator_asc",
        "progress_desc",
        "progress_asc",
    }
    if sort not in valid_sorts:
        sort = "created_desc"
    return {
        "q": params.get("q", "").strip(),
        "types": params.getlist("type"),
        "tags": params.getlist("tag"),
        "themes": params.getlist("theme"),
        "difficulty": params.getlist("difficulty"),
        "status": status,
        "completed": completed,
        "started": started,
        "created_from": params.get("created_from", ""),
        "created_to": params.get("created_to", ""),
        "archive_from": params.get("archive_from", ""),
        "archive_to": params.get("archive_to", ""),
        "archive_status": archive_status,
        "created_by": params.get("created_by", "any"),
        "availability": availability,
        "sort": sort,
        "printable": params.get("printable", "false").lower() in {"1", "true", "yes"},
        "limit": max(12, min(200, int(params.get("limit", "60") or 60))) if str(params.get("limit", "60")).isdigit() else 60,
        "current_user_id": current_user.id,
    }


def library_theme_label(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()


def library_theme_value(value: str) -> str:
    return slugify(library_theme_label(value))


def puzzle_theme_keys(puzzle: Puzzle) -> set[str]:
    keys = {puzzle.theme, slugify(puzzle.theme) if puzzle.theme else "", library_theme_value(puzzle.theme) if puzzle.theme else ""}
    if puzzle.category is not None:
        keys |= {str(puzzle.category.id), puzzle.category.slug, puzzle.category.name, library_theme_value(puzzle.category.name)}
    if puzzle.word_bank is not None:
        keys |= {puzzle.word_bank.slug, puzzle.word_bank.name, library_theme_value(puzzle.word_bank.name)}
    return {key for key in keys if key}


def creator_label(puzzle: Puzzle) -> str:
    return puzzle.creator.display_name if puzzle.creator is not None else "Unknown"


def puzzle_search_text(puzzle: Puzzle) -> str:
    parts = [
        puzzle.title,
        puzzle.prompt,
        puzzle.puzzle_type,
        puzzle.theme,
        puzzle.difficulty,
        puzzle.category.name if puzzle.category else "",
        puzzle.word_bank.name if puzzle.word_bank else "",
        creator_label(puzzle),
    ]
    parts.extend(tag.label for tag in puzzle.tags)
    return " ".join(part for part in parts if part).lower()


def active_filter_chips(filters: dict, options: dict) -> list[dict[str, str]]:
    chips: list[dict[str, str]] = []
    param_map = {
        "types": ("type", "Type", {item["value"]: item["label"] for item in options["types"]}),
        "tags": ("tag", "Tag", {item["value"]: item["label"] for item in options["tags"]}),
        "themes": ("theme", "Theme", {item["value"]: item["label"] for item in options["themes"]}),
        "difficulty": ("difficulty", "Difficulty", {item["value"]: item["label"] for item in options["difficulty"]}),
    }
    for key, (param, label, labels) in param_map.items():
        for value in filters[key]:
            chips.append({"param": param, "value": value, "label": f"{label}: {labels.get(value, value)}"})
    single_filters = [
        ("q", "Search", ""),
        ("status", "Status", "any"),
        ("completed", "Completed", "any"),
        ("started", "Started", "any"),
        ("created_from", "Created after", ""),
        ("created_to", "Created before", ""),
        ("archive_from", "Archives after", ""),
        ("archive_to", "Archives before", ""),
        ("archive_status", "Archive", "any"),
        ("created_by", "Creator", "any"),
        ("availability", "Availability", "active"),
        ("sort", "Sort", "created_desc"),
    ]
    creator_labels = {item["value"]: item["label"] for item in options["creators"]}
    sort_labels = {item["value"]: item["label"] for item in options["sorts"]}
    for key, label, default in single_filters:
        value = filters[key]
        if value == default or value == "":
            continue
        label_value = creator_labels.get(value, value) if key == "created_by" else sort_labels.get(value, value) if key == "sort" else value.replace("_", " ").title()
        chips.append({"param": key, "value": value, "label": f"{label}: {label_value}"})
    return chips


def query_without(request: Request, param: str, value: str | None = None) -> str:
    pairs = []
    for key, item in request.query_params.multi_items():
        if key == param and (value is None or item == value):
            continue
        pairs.append((key, item))
    query = urlencode(pairs, doseq=True)
    return f"/library?{query}" if query else "/library"


def safe_library_return_to(value: str) -> str:
    if not value.startswith("/library"):
        return "/library"
    if "://" in value or value.startswith("//"):
        return "/library"
    return value


def safe_return_to(value: str, default: str = "/library") -> str:
    if not value or "://" in value or value.startswith("//"):
        return default
    allowed_prefixes = ("/library", "/puzzles/", "/groups", "/generate", "/admin", "/account")
    if not value.startswith(allowed_prefixes):
        return default
    return value


def library_options(db: Session, puzzles: list[Puzzle], current_user: User) -> dict:
    visible_tag_ids = {tag.id for puzzle in puzzles for tag in puzzle.tags}
    tags = db.scalars(select(PuzzleTag).where(PuzzleTag.id.in_(visible_tag_ids)).order_by(PuzzleTag.tag_type, PuzzleTag.name)).all()
    users = db.scalars(select(User).order_by(User.display_name)).all()
    theme_values: dict[str, str] = {}
    for puzzle in puzzles:
        if puzzle.theme:
            theme_values.setdefault(library_theme_value(puzzle.theme), library_theme_label(puzzle.theme))
        if puzzle.category is not None:
            theme_values.setdefault(library_theme_value(puzzle.category.name), puzzle.category.name)
        if puzzle.word_bank is not None:
            theme_values.setdefault(library_theme_value(puzzle.word_bank.name), puzzle.word_bank.name)
    return {
        "types": [{"value": puzzle_type.slug, "label": puzzle_type.label} for puzzle_type in list_puzzle_types()],
        "tags": [{"value": str(tag.id), "label": tag.label} for tag in tags],
        "themes": [{"value": value, "label": label} for value, label in sorted(theme_values.items(), key=lambda item: item[1].lower())],
        "difficulty": [{"value": value, "label": value.title()} for value in ("easy", "medium", "hard")],
        "creators": [
            {"value": "me", "label": "Created by me"},
            {"value": "unknown", "label": "Unknown creator"},
        ] + [{"value": str(user.id), "label": user.display_name} for user in users],
        "sorts": [
            {"value": "created_desc", "label": "Newest first"},
            {"value": "created_asc", "label": "Oldest first"},
            {"value": "archive_asc", "label": "Archive soonest"},
            {"value": "archive_desc", "label": "Archive latest"},
            {"value": "title_asc", "label": "Title A-Z"},
            {"value": "title_desc", "label": "Title Z-A"},
            {"value": "type_asc", "label": "Puzzle type"},
            {"value": "difficulty_asc", "label": "Difficulty"},
            {"value": "creator_asc", "label": "Creator"},
            {"value": "progress_desc", "label": "Most progress"},
            {"value": "progress_asc", "label": "Least progress"},
        ],
    }


def filtered_library(
    db: Session,
    request: Request,
    current_user: User,
    include_archived_for_admin: bool = True,
) -> dict:
    def discoverable(puzzle: Puzzle) -> bool:
        if puzzle.archived_at is not None:
            return include_archived_for_admin and can_view(current_user, puzzle)
        return can_discover(current_user, puzzle)

    all_puzzles = db.scalars(select(Puzzle).options(
        selectinload(Puzzle.tags), selectinload(Puzzle.category), selectinload(Puzzle.word_bank),
        selectinload(Puzzle.creator),
    ).order_by(desc(Puzzle.created_at))).all()
    all_puzzles = [puzzle for puzzle in all_puzzles if discoverable(puzzle)]
    filters = library_filter_state(request, current_user)
    options = library_options(db, all_puzzles, current_user)
    progress_by_puzzle = progress_map_for_user(db, current_user, all_puzzles)
    now = utcnow()
    soon_cutoff = now + timedelta(hours=48)
    created_from = parse_date_boundary(filters["created_from"]) if filters["created_from"] else None
    created_to = parse_date_boundary(filters["created_to"], end_of_day=True) if filters["created_to"] else None
    archive_from = parse_date_boundary(filters["archive_from"]) if filters["archive_from"] else None
    archive_to = parse_date_boundary(filters["archive_to"], end_of_day=True) if filters["archive_to"] else None
    selected_tags = {int(tag_id) for tag_id in filters["tags"] if str(tag_id).isdigit()}

    def matches(puzzle: Puzzle) -> bool:
        if not discoverable(puzzle):
            return False
        if filters["availability"] == "active" and puzzle.archived_at is not None:
            return False
        if filters["availability"] == "archived" and puzzle.archived_at is None:
            return False
        if filters["availability"] == "scheduled" and puzzle.auto_archive_at is None:
            return False
        if filters["printable"]:
            puzzle_type = get_puzzle_type(puzzle.puzzle_type)
            if puzzle_type is None or not puzzle_type.supports_print or puzzle_type.digital_only:
                return False
            if not puzzle_access_granted(request, puzzle):
                return False
            try:
                require_series_access(db, puzzle, current_user)
            except HTTPException as error:
                if error.status_code == 423:
                    return False
                raise
        if not include_archived_for_admin and puzzle.archived_at is not None:
            return False
        if filters["types"] and puzzle.puzzle_type not in filters["types"]:
            return False
        if filters["difficulty"] and puzzle.difficulty not in filters["difficulty"]:
            return False
        if filters["themes"] and not (puzzle_theme_keys(puzzle) & set(filters["themes"])):
            return False
        if selected_tags and not ({tag.id for tag in puzzle.tags} & selected_tags):
            return False
        progress = progress_by_puzzle.get(puzzle.id, {"found": 0, "total": 0, "status": "not_started"})
        status = str(progress.get("status", "not_started"))
        if filters["status"] != "any" and status != filters["status"]:
            return False
        if filters["completed"] == "completed" and status != "completed":
            return False
        if filters["completed"] == "not_completed" and status == "completed":
            return False
        if filters["started"] == "started" and status == "not_started":
            return False
        if filters["started"] == "not_started" and status != "not_started":
            return False
        if created_from and puzzle.created_at < created_from:
            return False
        if created_to and puzzle.created_at > created_to:
            return False
        if archive_from and (puzzle.auto_archive_at is None or puzzle.auto_archive_at < archive_from):
            return False
        if archive_to and (puzzle.auto_archive_at is None or puzzle.auto_archive_at > archive_to):
            return False
        if filters["archive_status"] == "scheduled" and puzzle.auto_archive_at is None:
            return False
        if filters["archive_status"] == "none" and puzzle.auto_archive_at is not None:
            return False
        if filters["archive_status"] == "soon" and not (puzzle.archived_at is None and puzzle.auto_archive_at is not None and now <= puzzle.auto_archive_at <= soon_cutoff):
            return False
        if filters["created_by"] == "me" and puzzle.created_by_id != current_user.id:
            return False
        if filters["created_by"] == "unknown" and puzzle.created_by_id is not None:
            return False
        if filters["created_by"].isdigit() and puzzle.created_by_id != int(filters["created_by"]):
            return False
        if filters["q"] and filters["q"].lower() not in puzzle_search_text(puzzle):
            return False
        return True

    filtered = [puzzle for puzzle in all_puzzles if matches(puzzle)]
    sort = filters["sort"]
    difficulty_rank = {"easy": 1, "medium": 2, "hard": 3}

    def archive_key(puzzle: Puzzle) -> datetime:
        return puzzle.auto_archive_at or datetime.max.replace(tzinfo=timezone.utc)

    sorters = {
        "created_asc": lambda puzzle: (puzzle.created_at, puzzle.id),
        "archive_asc": lambda puzzle: (archive_key(puzzle), puzzle.title.lower()),
        "archive_desc": lambda puzzle: (archive_key(puzzle), puzzle.title.lower()),
        "title_asc": lambda puzzle: (puzzle.title.lower(), puzzle.id),
        "title_desc": lambda puzzle: (puzzle.title.lower(), puzzle.id),
        "type_asc": lambda puzzle: (puzzle.puzzle_type, puzzle.title.lower()),
        "difficulty_asc": lambda puzzle: (difficulty_rank.get(puzzle.difficulty, 99), puzzle.title.lower()),
        "creator_asc": lambda puzzle: (creator_label(puzzle).lower(), puzzle.title.lower()),
        "progress_desc": lambda puzzle: (int(progress_by_puzzle.get(puzzle.id, {}).get("found", 0)), puzzle.created_at),
        "progress_asc": lambda puzzle: (int(progress_by_puzzle.get(puzzle.id, {}).get("found", 0)), puzzle.created_at),
        "created_desc": lambda puzzle: (puzzle.created_at, puzzle.id),
    }
    reverse = sort in {"created_desc", "archive_desc", "title_desc", "progress_desc"}
    filtered.sort(key=sorters.get(sort, sorters["created_desc"]), reverse=reverse)
    limit = filters["limit"]
    visible = filtered[:limit]
    return {
        "puzzles": visible,
        "all_count": len(all_puzzles),
        "result_count": len(filtered),
        "has_more": len(filtered) > limit,
        "next_limit": min(200, limit + 24),
        "filters": filters,
        "options": options,
        "progress_by_puzzle": {puzzle.id: progress_by_puzzle[puzzle.id] for puzzle in visible if puzzle.id in progress_by_puzzle},
        "previews_by_puzzle": {
            puzzle.id: (
                puzzle_preview(puzzle)
                if puzzle_access_granted(request, puzzle)
                else {"kind": "locked", "label": "Password protected", "icon": "Lock", "meta": "Unlock to view"}
            )
            for puzzle in visible
        },
        "labels_by_type": {puzzle_type.slug: puzzle_type.label for puzzle_type in list_puzzle_types()},
    }


def serialize_library_item(puzzle: Puzzle, current_user: User, preview: dict, progress: dict | None, db: Session | None = None) -> dict:
    progress = progress or {"found": 0, "total": progress_response(puzzle, current_user, None).expected_count, "status": "not_started"}
    return {
        "id": puzzle.id,
        "title": puzzle.title,
        "puzzle_type": puzzle.puzzle_type,
        "puzzle_type_label": get_puzzle_type(puzzle.puzzle_type).label if get_puzzle_type(puzzle.puzzle_type) else puzzle.puzzle_type,
        "theme": puzzle.theme,
        "difficulty": puzzle.difficulty,
        "category": puzzle.category.name if puzzle.category else None,
        "tags": [serialize_tag(tag) for tag in puzzle.tags],
        "created_by": {
            "id": puzzle.creator.id if puzzle.creator else None,
            "display_name": creator_label(puzzle),
        },
        "created_at": puzzle.created_at.isoformat() if puzzle.created_at else None,
        "auto_archive_at": puzzle.auto_archive_at.isoformat() if puzzle.auto_archive_at else None,
        "publication_state": puzzle_publication_state(puzzle),
        "publish_at": puzzle.publish_at.isoformat() if puzzle.publish_at else None,
        "archived_at": puzzle.archived_at.isoformat() if puzzle.archived_at else None,
        "source": puzzle.source,
        "progress": progress,
        "preview": preview,
        "url": puzzle_url(puzzle.id, db),
        "password_protected": bool(puzzle.access_password_hash),
    }


def serialize_generation_field(field) -> dict:
    validation = {}
    if field.required:
        validation["required"] = True
    if field.minimum is not None:
        validation["minimum"] = field.minimum
    if field.maximum is not None:
        validation["maximum"] = field.maximum
    if field.options:
        validation["allowed_values"] = [option["value"] for option in field.options]
    return {
        "name": field.name,
        "label": field.label,
        "field_type": field.field_type,
        "default": field.default,
        "required": field.required,
        "help_text": field.help_text,
        "options": list(field.options),
        "minimum": field.minimum,
        "maximum": field.maximum,
        "enabled_when": dict(field.enabled_when),
        "validation": validation,
    }


def validate_generation_options(puzzle_type, options: dict, fields=None) -> dict:
    validated = dict(options or {})
    for field in (fields if fields is not None else puzzle_type.generation_fields):
        raw_value = validated.get(field.name)
        raw_values = raw_value if isinstance(raw_value, list) else ([raw_value] if raw_value not in (None, "") else [])
        values = [str(value).strip() for value in raw_values if str(value).strip()]
        if field.required and not values:
            raise HTTPException(status_code=400, detail=f"{field.label} is required")

        allowed_values = {str(option["value"]) for option in field.options}
        if allowed_values and values:
            invalid_values = [value for value in values if value not in allowed_values]
            if invalid_values:
                raise HTTPException(status_code=400, detail=f"{field.label} has an unsupported value")

        if field.field_type == "number" and values:
            try:
                numeric_value = int(values[0])
            except ValueError as error:
                raise HTTPException(status_code=400, detail=f"{field.label} must be a number") from error
            if field.minimum is not None and numeric_value < field.minimum:
                raise HTTPException(status_code=400, detail=f"{field.label} must be at least {field.minimum}")
            if field.maximum is not None and numeric_value > field.maximum:
                raise HTTPException(status_code=400, detail=f"{field.label} must be no more than {field.maximum}")
    return validated


def serialize_puzzle_type_contract(puzzle_type, enabled: bool = True) -> dict:
    needs_word_bank = "word_bank" in puzzle_type.source_requirements
    return {
        "slug": puzzle_type.slug,
        "label": puzzle_type.label,
        "description": puzzle_type.description,
        "enabled": enabled,
        "supports_print": puzzle_type.supports_print,
        "supports_web_play": puzzle_type.supports_web_play,
        "supports_group_solve": puzzle_type.collaboration is not None,
        "supports_offline": puzzle_type.supports_offline,
        "offline_progress_fields": list(puzzle_type.offline_progress_fields),
        "offline_notes": puzzle_type.offline_notes,
        "offline_validation": puzzle_type.offline_validation,
        "offline_hash_mode": puzzle_type.offline_hash_mode,
        "offline_has_game_data": puzzle_type.offline_game_data is not None,
        "offline_has_solution_parts": puzzle_type.offline_solution_parts is not None,
        "collaboration_fields": [serialize_generation_field(field) for field in puzzle_type.collaboration_fields],
        "digital_only": puzzle_type.digital_only,
        "supports_gimmes": puzzle_type.supports_gimmes,
        "gimme_modes": list(puzzle_type.gimme_modes),
        "needs_word_bank": needs_word_bank,
        "theme_mode": puzzle_type.theme_mode,
        "source_requirements": list(puzzle_type.source_requirements),
        "word_bank_fields": list(puzzle_type.word_bank_fields) if needs_word_bank else [],
        "word_bank_required_fields": list(puzzle_type.word_bank_required_fields) if needs_word_bank else [],
        "generation_fields": [serialize_generation_field(field) for field in puzzle_type.generation_fields],
        "manual_editor_fields": [serialize_generation_field(field) for field in puzzle_type.manual_editor_fields],
        "manual_editor_script": puzzle_type.manual_editor_script,
        "play_script": puzzle_type.play_script,
        "supports_manual_editor": puzzle_type.manual_builder is not None,
        "print_layout": puzzle_type.print_layout.as_dict(),
        "templates": {
            "play": f"puzzle_types/{puzzle_type.slug}.html",
            "solution": f"puzzle_solutions/{puzzle_type.slug}.html",
            "print": f"puzzle_print/{puzzle_type.slug}.html",
        },
    }


def puzzle_type_generation_schemas(db: Session | None = None, include_disabled: bool = False) -> dict[str, dict]:
    schemas = {}
    disabled = disabled_puzzle_type_slugs(db) if db is not None else set()
    for puzzle_type in list_puzzle_types():
        enabled = puzzle_type.slug not in disabled
        if not enabled and not include_disabled:
            continue
        schemas[puzzle_type.slug] = serialize_puzzle_type_contract(puzzle_type, enabled)
    return schemas


def generation_options_from_form(form) -> dict:
    options = {}
    for key, value in form.multi_items():
        if not key.startswith("gen_"):
            continue
        option_key = key.removeprefix("gen_")
        value = str(value).strip()
        if value:
            if option_key in options:
                if not isinstance(options[option_key], list):
                    options[option_key] = [options[option_key]]
                options[option_key].append(value)
            else:
                options[option_key] = value
    return options


def prefixed_options_from_form(form, prefix: str) -> dict:
    options = {}
    for key, value in form.multi_items():
        if not key.startswith(prefix):
            continue
        option_key = key.removeprefix(prefix)
        value = str(value).strip()
        if value:
            if option_key in options:
                if not isinstance(options[option_key], list):
                    options[option_key] = [options[option_key]]
                options[option_key].append(value)
            else:
                options[option_key] = value
    return options


def build_manual_payload_from_helper(
    db: Session,
    puzzle_type_slug: str,
    title: str,
    theme: str,
    difficulty: str,
    completion_message: str,
    draft: str,
    form,
    visibility: str = "public",
    access_password: str = "",
    remove_access_password: bool = False,
    publish_at: str = "",
    offline_allowed: bool = True,
) -> PuzzleManualWrite:
    puzzle_type = get_puzzle_type(puzzle_type_slug)
    if puzzle_type is None or not is_puzzle_type_enabled(db, puzzle_type_slug):
        raise ValueError("Unsupported puzzle type")
    if puzzle_type.manual_builder is None:
        raise ValueError("This puzzle type does not have a GUI editor helper yet")
    try:
        manual_options = validate_generation_options(
            puzzle_type,
            prefixed_options_from_form(form, "manual_"),
            fields=puzzle_type.manual_editor_fields,
        )
    except HTTPException as error:
        raise ValueError(str(error.detail)) from error
    manual_options["_theme"] = theme.strip() or "manual"
    manual_options["_difficulty"] = difficulty.strip() or "manual"
    generated = puzzle_type.manual_builder(manual_options)
    metadata = dict(generated.metadata or {})
    metadata["manual_editor"] = True
    metadata["manual_editor_options"] = manual_options
    return PuzzleManualWrite(
        puzzle_type=puzzle_type_slug,
        title=(title.strip() or generated.title)[:160],
        theme=(theme.strip() or "manual")[:80],
        difficulty=(difficulty.strip() or "manual")[:40],
        completion_message=normalize_completion_message(completion_message),
        prompt=generated.prompt,
        print_payload=generated.print_payload,
        play_payload=generated.play_payload,
        solution_payload=generated.solution_payload,
        metadata=metadata,
        hints=generated.hints,
        draft=draft == "true",
        visibility="unlisted" if visibility == "unlisted" else "public",
        access_password=access_password,
        remove_access_password=remove_access_password,
        publish_at=parse_local_datetime(publish_at, "Publication date/time") if publish_at.strip() else None,
        offline_allowed=offline_allowed,
    )


def printable_gimme_cells(puzzle: Puzzle) -> dict[str, str]:
    print_payload = puzzle.print_payload if isinstance(puzzle.print_payload, dict) else {}
    solution_payload = puzzle.solution_payload if isinstance(puzzle.solution_payload, dict) else {}
    gimmes = print_payload.get("gimmes") if isinstance(print_payload.get("gimmes"), dict) else solution_payload.get("gimmes", {})
    if not isinstance(gimmes, dict):
        return {}

    if print_payload.get("kind") == "crossword":
        cells = gimmes.get("cells", {})
        if not isinstance(cells, dict):
            return {}
        return {str(key): str(value).upper()[:1] for key, value in cells.items() if str(value).strip()}

    if print_payload.get("kind") == "word_search":
        gimme_words = {str(word).upper() for word in gimmes.get("words", []) if str(word).strip()}
        cells = {}
        for placement in solution_payload.get("placements", []):
            word = str(placement.get("word", "")).upper()
            if word not in gimme_words:
                continue
            start = placement.get("start", [])
            direction = placement.get("direction", [])
            if len(start) != 2 or len(direction) != 2:
                continue
            x, y = int(start[0]), int(start[1])
            dx, dy = int(direction[0]), int(direction[1])
            for index, letter in enumerate(word):
                cells[f"{x + dx * index},{y + dy * index}"] = letter
        return cells

    return {}


def printable_gimme_words(puzzle: Puzzle) -> list[str]:
    print_payload = puzzle.print_payload if isinstance(puzzle.print_payload, dict) else {}
    solution_payload = puzzle.solution_payload if isinstance(puzzle.solution_payload, dict) else {}
    gimmes = print_payload.get("gimmes") if isinstance(print_payload.get("gimmes"), dict) else solution_payload.get("gimmes", {})
    if not isinstance(gimmes, dict):
        return []
    return sorted({str(word).upper() for word in gimmes.get("words", []) if str(word).strip()})


def serialize_tag(tag: PuzzleTag) -> dict:
    return {
        "id": tag.id,
        "type": tag.tag_type,
        "name": tag.name,
        "slug": tag.slug,
        "label": tag.label,
    }


def serialize_definition(definition: DictionaryDefinition) -> dict:
    return {
        "id": definition.id,
        "definition": definition.definition,
        "part_of_speech": definition.part_of_speech,
        "clues": stored_definition_clues(definition),
        "example": definition.example,
        "source": definition.source,
        "position": definition.position,
        "properties": definition.properties or {},
        "archived": definition.archived_at is not None,
        "archived_at": definition.archived_at.isoformat() if definition.archived_at else None,
        "tags": [serialize_tag(tag) for tag in definition.tags],
    }


def serialize_dictionary_word(word: DictionaryWord) -> dict:
    return {
        "id": word.id,
        "word": word.word,
        "display_text": word.display_text,
        "clue": word.clue,
        "part_of_speech": word.part_of_speech,
        "properties": word.properties or {},
        "archived": word.archived_at is not None,
        "banned": word.banned_at is not None,
        "tags": [serialize_tag(tag) for tag in word.tags],
        "definitions": [serialize_definition(definition) for definition in word.definitions],
    }


def dictionary_export_payload(words: list[DictionaryWord]) -> dict:
    return {
        "schema": "puzzary.dictionary.v1",
        "exported_at": utcnow().isoformat(),
        "total": len(words),
        "items": [serialize_dictionary_word(word) for word in words],
    }


def serialize_word_bank_link(link: WordBankWord) -> dict:
    return {
        "id": link.id,
        "word_id": link.word_id,
        "definition_id": link.definition_id,
        "position": link.position,
        "word": serialize_dictionary_word(link.word) if link.word is not None else None,
        "definition": serialize_definition(link.definition) if link.definition is not None else None,
    }


def serialize_word_bank(bank: WordBank, include_words: bool = False) -> dict:
    payload = bank.payload if isinstance(bank.payload, dict) else {}
    data = {
        "id": bank.id,
        "name": bank.name,
        "slug": bank.slug,
        "description": bank.description,
        "mode": word_bank_mode(bank),
        "category_id": bank.category_id,
        "category": bank.category.name if bank.category else None,
        "tags": [serialize_tag(tag) for tag in bank.tags],
        "properties": payload.get("properties", {}),
        "archived": bank.archived_at is not None,
        "archived_at": bank.archived_at.isoformat() if bank.archived_at else None,
        "created_at": bank.created_at.isoformat() if bank.created_at else None,
    }
    if include_words:
        data["entries"] = [serialize_word_bank_link(link) for link in sorted(bank.word_links, key=lambda item: item.position)]
    else:
        data["entry_count"] = len(bank.word_links)
    return data


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or uuid.uuid4().hex[:8]


def unique_slug(db: Session, model: type, value: str, ignore_id: int | None = None) -> str:
    base_slug = slugify(value)
    slug = base_slug
    suffix = 2
    while True:
        existing = db.scalar(select(model).where(model.slug == slug))
        if existing is None or existing.id == ignore_id:
            return slug
        slug = f"{base_slug}_{suffix}"
        suffix += 1


def parse_words(value: str) -> list[str]:
    words = []
    for part in re.split(r"[\n,;]+", value):
        word = re.sub(r"[^A-Za-z0-9]+", "", part).upper()
        if word and word not in words:
            words.append(word)
    return words


def normalize_word(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", value).upper()


def normalize_dictionary_word(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", value).lower()


def normalize_definition_signature(definition: str, part_of_speech: str = "") -> str:
    normalized_definition = re.sub(r"\s+", " ", definition.strip().lower())
    normalized_part = re.sub(r"\s+", " ", part_of_speech.strip().lower())
    return f"{normalized_part}|{normalized_definition}"


def import_dictionary_words(
    db: Session,
    imported_words: list[ImportedWord],
    source_label: str = "",
    tag_ids: list[int] | None = None,
    dry_run: bool = True,
    max_items: int = 500,
) -> dict:
    import_label = source_label.strip() or "External Import"
    imported_at = datetime.now(timezone.utc).isoformat()
    selected_tags = tags_for_ids(db, tag_ids or [])
    source_tag = get_or_create_tag(db, "Source", import_label) if not dry_run else None
    if source_tag is not None and all(tag.id != source_tag.id for tag in selected_tags):
        selected_tags.append(source_tag)

    result = {
        "dry_run": dry_run,
        "source_label": import_label,
        "fetched_words": len(imported_words),
        "processed_words": 0,
        "added_words": 0,
        "merged_words": 0,
        "skipped_words": 0,
        "added_definitions": 0,
        "skipped_duplicate_definitions": 0,
        "sample_words": [],
        "errors": [],
    }

    for imported in imported_words[: max(1, min(max_items, 5000))]:
        word_key = normalize_dictionary_word(imported.word)
        if not word_key:
            result["skipped_words"] += 1
            continue
        result["processed_words"] += 1
        existing = db.scalar(select(DictionaryWord).where(DictionaryWord.word == word_key))
        word_exists = existing is not None
        if not dry_run and existing is None:
            existing = DictionaryWord(
                word=word_key,
                display_text=imported.display_text.strip() or imported.word.strip(),
                clue=imported.clue.strip(),
                part_of_speech=imported.part_of_speech.strip(),
                properties={**(imported.properties or {}), "import_source": import_label, "imported_at": imported_at},
            )
            existing.tags = list(selected_tags)
            db.add(existing)
            db.flush()
        elif not dry_run and existing is not None:
            if not existing.display_text and imported.display_text.strip():
                existing.display_text = imported.display_text.strip()
            if not existing.clue and imported.clue.strip():
                existing.clue = imported.clue.strip()
            if not existing.part_of_speech and imported.part_of_speech.strip():
                existing.part_of_speech = imported.part_of_speech.strip()
            existing_tag_ids = {tag.id for tag in existing.tags}
            for tag in selected_tags:
                if tag.id not in existing_tag_ids:
                    existing.tags.append(tag)
                    existing_tag_ids.add(tag.id)

        if word_exists:
            result["merged_words"] += 1
        else:
            result["added_words"] += 1
        if len(result["sample_words"]) < 10:
            result["sample_words"].append(
                {
                    "word": imported.display_text or imported.word,
                    "action": "merge" if word_exists else "add",
                    "definitions": len(imported.definitions),
                }
            )

        existing_signatures = set()
        if existing is not None:
            existing_signatures = {
                normalize_definition_signature(definition.definition, definition.part_of_speech)
                for definition in existing.definitions
            }
        seen_import_signatures: set[str] = set()
        for index, imported_definition in enumerate(imported.definitions, start=1):
            signature = normalize_definition_signature(
                imported_definition.definition,
                imported_definition.part_of_speech or imported.part_of_speech,
            )
            if not signature.strip("|") or signature in existing_signatures or signature in seen_import_signatures:
                result["skipped_duplicate_definitions"] += 1
                continue
            seen_import_signatures.add(signature)
            result["added_definitions"] += 1
            if dry_run or existing is None:
                continue
            definition = DictionaryDefinition(
                word=existing,
                definition=imported_definition.definition.strip(),
                part_of_speech=(imported_definition.part_of_speech or imported.part_of_speech).strip(),
                example=imported_definition.example.strip(),
                source=import_label[:120],
                position=max((item.position for item in existing.definitions), default=0) + index,
                properties={
                    **(imported_definition.properties or {}),
                    "import_source": import_label,
                    "imported_at": imported_at,
                    "clues": [clue.strip() for clue in imported_definition.clues if clue.strip()],
                },
            )
            definition.tags = definition_tags_from_form(
                db,
                [tag.id for tag in selected_tags],
                definition.part_of_speech,
                "\n".join(imported_definition.clues),
            )
            for tag_name in imported_definition.tags:
                tag = get_or_create_tag(db, "Imported", tag_name)
                if tag is not None and all(existing_tag.id != tag.id for existing_tag in definition.tags):
                    definition.tags.append(tag)
            db.add(definition)

    if not dry_run:
        db.commit()
    return result


def normalize_seed(value: str | None) -> str:
    if value and value.strip():
        return value.strip()
    return uuid.uuid4().hex[:12]


def word_bank_words(bank: WordBank | None) -> list[str]:
    if bank is None:
        return []
    linked_words = [link.word.word for link in sorted(bank.word_links, key=lambda item: item.position)]
    if linked_words:
        return linked_words
    words = bank.payload.get("words", [])
    if not isinstance(words, list):
        return []
    return [str(word).upper() for word in words if str(word).strip()]


def word_bank_mode(bank: WordBank | None) -> str:
    if bank is None or not isinstance(bank.payload, dict):
        return "manual"
    return str(bank.payload.get("mode") or "manual")


def definition_clue(definition: DictionaryDefinition | None) -> str:
    if definition is None:
        return ""
    clues = stored_definition_clues(definition)
    return clues[0] if clues else definition.definition


templates.env.globals["word_bank_mode"] = word_bank_mode
templates.env.globals["definition_clue"] = definition_clue


def word_bank_entries(db: Session, bank: WordBank | None, excluded_tag_ids: set[int] | None = None, dictionary_words: list[DictionaryWord] | None = None) -> list[dict]:
    if bank is None:
        return []
    excluded_tag_ids = excluded_tag_ids or set()
    entries: list[dict] = []
    if word_bank_mode(bank) == "automatic":
        tag_ids = {tag.id for tag in bank.tags}
        if not tag_ids:
            return []
        words = dictionary_words if dictionary_words is not None else db.scalars(dictionary_statement()).all()
        for word in words:
            if word.archived_at is not None or word.banned_at is not None:
                continue
            word_tag_ids = {tag.id for tag in word.tags}
            definitions = sorted(
                [definition for definition in word.definitions if definition.archived_at is None],
                key=lambda item: item.position,
            )
            matched_definitions = [
                definition
                for definition in definitions
                if tag_ids & ({tag.id for tag in definition.tags} | word_tag_ids)
            ]
            if not matched_definitions and tag_ids & word_tag_ids:
                matched_definitions = [None]
            for definition in matched_definitions:
                if definition is not None and excluded_tag_ids & {tag.id for tag in definition.tags}:
                    continue
                if excluded_tag_ids & word_tag_ids:
                    continue
                entries.append({"word": word, "definition": definition, "properties": {**(word.properties or {}), **((definition.properties or {}) if definition else {})}})
        return entries

    for link in sorted(bank.word_links, key=lambda item: item.position):
        word = link.word
        if word.archived_at is not None or word.banned_at is not None:
            continue
        if excluded_tag_ids & {tag.id for tag in word.tags}:
            continue
        definition = link.definition
        if definition is not None and definition.archived_at is not None:
            continue
        if definition is not None and excluded_tag_ids & {tag.id for tag in definition.tags}:
            continue
        entries.append({"word": word, "definition": definition, "properties": {**(word.properties or {}), **((definition.properties or {}) if definition else {})}})
    return entries


def get_or_create_word(db: Session, value: str) -> DictionaryWord | None:
    word = normalize_word(value)
    if not word:
        return None
    existing = db.scalar(select(DictionaryWord).where(DictionaryWord.word == word))
    if existing is not None:
        return existing
    entry = DictionaryWord(word=word, display_text=word)
    db.add(entry)
    db.flush()
    return entry


def sync_word_bank_words(db: Session, bank: WordBank, words: list[str]) -> None:
    bank.word_links.clear()
    db.flush()
    for position, word_value in enumerate(words):
        entry = get_or_create_word(db, word_value)
        if entry is not None:
            bank.word_links.append(WordBankWord(word=entry, position=position))


def add_word_bank_link(db: Session, bank: WordBank, word: DictionaryWord, definition: DictionaryDefinition | None = None) -> None:
    if any(link.word_id == word.id for link in bank.word_links):
        return
    next_position = max((link.position for link in bank.word_links), default=-1) + 1
    bank.word_links.append(WordBankWord(word=word, definition=definition, position=next_position))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_local_datetime(value: str, label: str = "Date/time") -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"{label} is not valid") from error
    if parsed.tzinfo is None:
        zone = app_timezone()
        localized = parsed.replace(tzinfo=zone)
        round_trip = localized.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if round_trip != parsed:
            raise HTTPException(status_code=422, detail=f"{label} falls in a daylight-saving time gap")
        parsed = localized
    return parsed.astimezone(timezone.utc)


def normalize_publish_at(value: datetime | str | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return parse_local_datetime(value, "Publication date/time")
    if value.tzinfo is None:
        value = value.replace(tzinfo=app_timezone())
    return value.astimezone(timezone.utc)


def require_future_publish_at(value: datetime | str | None) -> datetime | None:
    publish_at = normalize_publish_at(value)
    if publish_at is not None and publish_at <= utcnow():
        raise HTTPException(422, "Do not publish until must be in the future; publish now instead")
    return publish_at


def schedule_puzzle(db: Session, actor: User, puzzle: Puzzle, value: datetime | str | None) -> None:
    before_state = puzzle_publication_state(puzzle)
    before_time = puzzle.publish_at.isoformat() if puzzle.publish_at else None
    publish_at = require_future_publish_at(value)
    if publish_at is None:
        if before_state != "scheduled":
            return
        metadata = dict(puzzle.puzzle_metadata or {})
        metadata["draft"] = True
        puzzle.puzzle_metadata = metadata
        puzzle.publish_at = None
        puzzle.archived_at = utcnow()
        puzzle.auto_archive_at = None
        audit(db, actor, "puzzle", puzzle.id, "puzzle.schedule_canceled", {
            "state": before_state, "publish_at": before_time,
        }, {"state": "draft", "publish_at": None})
        return
    if before_state not in {"draft", "scheduled"}:
        raise HTTPException(409, "Only a draft or scheduled puzzle can be scheduled")
    metadata = dict(puzzle.puzzle_metadata or {})
    metadata["draft"] = False
    puzzle.puzzle_metadata = metadata
    puzzle.publish_at = publish_at
    puzzle.archived_at = None
    archive_at = normalize_publish_at(puzzle.auto_archive_at)
    if archive_at is None or archive_at <= publish_at:
        puzzle.auto_archive_at = default_auto_archive_at(publish_at)
    audit(db, actor, "puzzle", puzzle.id,
          "puzzle.rescheduled" if before_state == "scheduled" else "puzzle.scheduled",
          {"state": before_state, "publish_at": before_time},
          {"state": "scheduled", "publish_at": publish_at.isoformat()})


def publish_puzzle_now(db: Session, actor: User, puzzle: Puzzle) -> None:
    before = {
        "state": puzzle_publication_state(puzzle),
        "publish_at": puzzle.publish_at.isoformat() if puzzle.publish_at else None,
    }
    metadata = dict(puzzle.puzzle_metadata or {})
    if puzzle.source == "manual":
        metadata["manual"] = True
        metadata["manual_published_at"] = utcnow().isoformat()
    metadata["draft"] = False
    puzzle.puzzle_metadata = metadata
    puzzle.publish_at = None
    puzzle.archived_at = None
    archive_at = normalize_publish_at(puzzle.auto_archive_at)
    if archive_at is None or archive_at <= utcnow():
        puzzle.auto_archive_at = default_auto_archive_at()
    audit(db, actor, "puzzle", puzzle.id, "puzzle.published", before, {
        "state": "published", "publish_at": None,
    })
    puzzle_notifications.queue_puzzle_publication(db, puzzle)


def apply_due_auto_archives(db: Session) -> int:
    now = utcnow()
    due_puzzles = db.scalars(
        select(Puzzle).where(
            Puzzle.archived_at.is_(None),
            Puzzle.auto_archive_at.is_not(None),
            Puzzle.auto_archive_at <= now,
            or_(Puzzle.publish_at.is_(None), Puzzle.publish_at <= now),
        )
    ).all()
    for puzzle in due_puzzles:
        puzzle.archived_at = now
    if due_puzzles:
        db.commit()
    return len(due_puzzles)


def dictionary_import_source_summary(source: DictionaryImportSource) -> dict:
    return {
        "id": source.id,
        "name": source.name,
        "source_url": source.source_url,
        "source_type": source.source_type,
        "source_label": source.source_label,
        "sync_interval_hours": source.sync_interval_hours,
        "max_items": source.max_items,
        "enabled": source.enabled,
        "has_api_token": bool(source.source_api_token),
        "last_synced_at": source.last_synced_at.isoformat() if source.last_synced_at else None,
        "next_sync_at": source.next_sync_at.isoformat() if source.next_sync_at else None,
        "last_result": source.last_result or {},
        "created_at": source.created_at.isoformat() if source.created_at else None,
    }


def schedule_next_dictionary_import(source: DictionaryImportSource, now: datetime | None = None) -> None:
    base = now or utcnow()
    interval = max(1, min(int(source.sync_interval_hours or 24), 24 * 30))
    source.next_sync_at = base + timedelta(hours=interval)


def run_dictionary_import_source(db: Session, source: DictionaryImportSource, dry_run: bool = False) -> dict:
    now = utcnow()
    try:
        sources = fetch_dictionary_source(source.source_url, source.source_type, source.source_api_token)
        imported_words = parse_imported_words(sources)
        result = import_dictionary_words(
            db,
            imported_words,
            source_label=source.source_label or source.name or source.source_url,
            tag_ids=[],
            dry_run=dry_run,
            max_items=source.max_items,
        )
        result["source_count"] = len(sources)
        result["source_url"] = source.source_url
        result["source_type"] = source.source_type
        result["synced_at"] = now.isoformat()
    except ValueError as error:
        result = {
            "dry_run": dry_run,
            "source_url": source.source_url,
            "source_type": source.source_type,
            "errors": [str(error)],
            "synced_at": now.isoformat(),
        }
    source.last_synced_at = now
    source.last_result = result
    schedule_next_dictionary_import(source, now)
    db.commit()
    return result


def run_due_dictionary_import_sources(db: Session) -> int:
    now = utcnow()
    sources = db.scalars(
        select(DictionaryImportSource).where(
            DictionaryImportSource.enabled == True,  # noqa: E712
            (DictionaryImportSource.next_sync_at.is_(None)) | (DictionaryImportSource.next_sync_at <= now),
        )
    ).all()
    for source in sources:
        run_dictionary_import_source(db, source, dry_run=False)
    return len(sources)


async def auto_archive_loop() -> None:
    while True:
        with SessionLocal() as db:
            apply_due_auto_archives(db)
        await asyncio.sleep(60)


async def dictionary_import_source_loop() -> None:
    while True:
        with SessionLocal() as db:
            run_due_dictionary_import_sources(db)
        await asyncio.sleep(DICTIONARY_SYNC_CHECK_SECONDS)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_api_token() -> str:
    return f"pzy_{secrets.token_urlsafe(32)}"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PASSWORD_ITERATIONS)
    return f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}${salt}${derived.hex()}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    try:
        scheme, iterations_text, salt, digest = stored_hash.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations_text))
        return hmac.compare_digest(derived.hex(), digest)
    except (TypeError, ValueError):
        return False


def password_error(password: str) -> str:
    if len(password) < 10:
        return "Use at least 10 characters."
    if password.lower() == password or password.upper() == password:
        return "Use a mix of uppercase and lowercase letters."
    if not any(char.isdigit() for char in password):
        return "Add at least one number."
    return ""


def puzzle_password_key(request: Request, puzzle: Puzzle) -> tuple[str, str]:
    client = request.client.host if request.client else "unknown"
    user_id = getattr(request.state, "puzzary_user_id", None) or request.session.get("user_id", "guest")
    return puzzle.id, f"{client}:{user_id}"


def puzzle_unlock_fingerprint(puzzle: Puzzle) -> str:
    return hash_secret(puzzle.access_password_hash or "")


def remember_puzzle_unlock(request: Request, puzzle: Puzzle) -> None:
    unlocks = dict(request.session.get("puzzle_unlocks", {}))
    if len(unlocks) >= 100 and puzzle.id not in unlocks:
        unlocks.pop(next(iter(unlocks)))
    unlocks[puzzle.id] = puzzle_unlock_fingerprint(puzzle)
    request.session["puzzle_unlocks"] = unlocks


def puzzle_access_granted(request: Request, puzzle: Puzzle, password: str = "") -> bool:
    if not puzzle.access_password_hash:
        return True
    unlocks = request.session.get("puzzle_unlocks", {})
    if isinstance(unlocks, dict) and hmac.compare_digest(
        str(unlocks.get(puzzle.id, "")), puzzle_unlock_fingerprint(puzzle)
    ):
        return True
    supplied = password or request.headers.get("x-puzzle-password", "")
    if supplied and verify_password(supplied, puzzle.access_password_hash):
        remember_puzzle_unlock(request, puzzle)
        with PUZZLE_PASSWORD_ATTEMPT_LOCK:
            PUZZLE_PASSWORD_ATTEMPTS.pop(puzzle_password_key(request, puzzle), None)
        return True
    return False


def require_puzzle_access(request: Request, puzzle: Puzzle, password: str = "") -> None:
    if puzzle_access_granted(request, puzzle, password):
        return
    key = puzzle_password_key(request, puzzle)
    now = time.monotonic()
    with PUZZLE_PASSWORD_ATTEMPT_LOCK:
        attempts = PUZZLE_PASSWORD_ATTEMPTS.setdefault(key, deque())
        while attempts and now - attempts[0] >= PUZZLE_PASSWORD_ATTEMPT_WINDOW:
            attempts.popleft()
        if len(attempts) >= PUZZLE_PASSWORD_ATTEMPT_LIMIT:
            retry = max(1, int(PUZZLE_PASSWORD_ATTEMPT_WINDOW - (now - attempts[0])))
            raise HTTPException(429, "Too many incorrect puzzle password attempts. Try again shortly.", headers={"Retry-After": str(retry)})
        if password or request.headers.get("x-puzzle-password", ""):
            attempts.append(now)
    raise HTTPException(423, "Puzzle access password required or incorrect")


def update_puzzle_access_password(puzzle: Puzzle, password: str = "", remove: bool = False) -> None:
    if password and remove:
        raise ValueError("Enter a new puzzle password or remove the existing password, not both")
    if password:
        error = password_error(password)
        if error:
            raise ValueError(error)
        puzzle.access_password_hash = hash_password(password)
    elif remove:
        puzzle.access_password_hash = None
    if password or remove:
        with PUZZLE_PASSWORD_ATTEMPT_LOCK:
            for key in [key for key in PUZZLE_PASSWORD_ATTEMPTS if key[0] == puzzle.id]:
                PUZZLE_PASSWORD_ATTEMPTS.pop(key, None)


def request_puzzle(request: Request, db: Session) -> Puzzle | None:
    puzzle_id = request.path_params.get("puzzle_id")
    if puzzle_id:
        return db.get(Puzzle, puzzle_id)
    group_id = request.path_params.get("group_id")
    if group_id:
        from app.models import GroupSolve
        group = db.get(GroupSolve, group_id)
        return db.get(Puzzle, group.puzzle_id) if group else None
    return None


GUEST_LINK_ACTIVE_MINUTES = 20


def puzzle_guest_link_eligible(puzzle: Puzzle | None) -> bool:
    if puzzle is None or puzzle.archived_at is not None:
        return False
    if puzzle.auto_archive_at is not None and (puzzle.auto_archive_at.replace(tzinfo=timezone.utc) if puzzle.auto_archive_at.tzinfo is None else puzzle.auto_archive_at) <= utcnow():
        return False
    return puzzle_is_published(puzzle)


def guest_link_is_valid(link: PuzzleGuestLink, puzzle: Puzzle | None = None) -> bool:
    if link.revoked_at is not None:
        return False
    if link.expires_at is not None and (link.expires_at.replace(tzinfo=timezone.utc) if link.expires_at.tzinfo is None else link.expires_at) <= utcnow():
        return False
    return puzzle_guest_link_eligible(puzzle or link.puzzle)


templates.env.globals["guest_link_is_valid"] = guest_link_is_valid


def guest_link_from_token(db: Session, token: str, for_update: bool = False) -> PuzzleGuestLink | None:
    statement = select(PuzzleGuestLink).where(PuzzleGuestLink.token_hash == hash_secret(token))
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def guest_link_user_for_puzzle(
    request: Request,
    db: Session,
    puzzle_id: str,
    require_group_solve: bool = False,
):
    sessions = request.session.get("puzzle_guest_links", {})
    raw_token = sessions.get(puzzle_id, "") if isinstance(sessions, dict) else ""
    if not raw_token:
        return None
    guest_session = db.scalar(select(PuzzleGuestSession).where(
        PuzzleGuestSession.token_hash == hash_secret(raw_token),
        PuzzleGuestSession.revoked_at.is_(None),
    ))
    if guest_session is None:
        return None
    link = db.get(PuzzleGuestLink, guest_session.link_id)
    puzzle = db.get(Puzzle, puzzle_id)
    if link is None or link.puzzle_id != puzzle_id or not guest_link_is_valid(link, puzzle):
        return None
    if require_group_solve and not link.allow_group_solve:
        return None
    guest_session.last_seen_at = utcnow()
    db.commit()
    permissions = {"library.view", "puzzles.play"}
    if link.allow_group_solve:
        permissions.add("groups.join")
    return SimpleNamespace(
        id=-(1_000_000 + guest_session.id),
        display_name=guest_session.display_name,
        is_admin=False,
        is_guest=True,
        is_guest_link=True,
        guest_permissions=permissions,
        guest_session=guest_session,
        guest_link=link,
        can=lambda permission: permission in permissions,
    )


def progress_record_for_user(db: Session, puzzle: Puzzle, user, create: bool = False):
    if getattr(user, "is_guest_link", False):
        return user.guest_session
    progress = db.scalar(select(PuzzleProgress).where(PuzzleProgress.puzzle_id == puzzle.id, PuzzleProgress.user_id == user.id))
    if progress is None and create:
        progress = PuzzleProgress(puzzle_id=puzzle.id, user_id=user.id, progress_payload={})
        db.add(progress)
    return progress


def series_entry_available(entry: PuzzleSeriesEntry) -> bool:
    puzzle = entry.puzzle
    if puzzle is None or not puzzle_is_published(puzzle):
        return False
    archive_at = puzzle.auto_archive_at
    if archive_at is not None and archive_at.tzinfo is None:
        archive_at = archive_at.replace(tzinfo=timezone.utc)
    return archive_at is None or utcnow() < archive_at


def puzzle_progress_complete(db: Session, puzzle: Puzzle, user) -> bool:
    if getattr(user, "is_guest_link", False) and user.guest_link.puzzle_id != puzzle.id:
        return False
    progress = progress_record_for_user(db, puzzle, user)
    plugin = get_puzzle_type(puzzle.puzzle_type)
    if plugin is not None and progress is not None:
        found, total = plugin.progress_counter(progress.progress_payload or {}, puzzle.solution_payload)
        if total > 0 and found >= total:
            return True
    if getattr(user, "is_guest", False):
        return False
    return db.scalar(select(PuzzleAttempt.id).where(
        PuzzleAttempt.puzzle_id == puzzle.id,
        PuzzleAttempt.user_id == user.id,
        PuzzleAttempt.is_correct == True,  # noqa: E712
    ).limit(1)) is not None


def group_puzzle_complete(group, puzzle: Puzzle) -> bool:
    state = group.state if group.puzzle_id == puzzle.id else (group.puzzle_states or {}).get(puzzle.id)
    plugin = get_puzzle_type(puzzle.puzzle_type)
    if state is None or plugin is None:
        return False
    found, total = plugin.progress_counter(state, puzzle.solution_payload)
    return total > 0 and found >= total


def series_admin_bypass(user, series: PuzzleSeries) -> bool:
    return not getattr(user, "is_guest", False) and (
        user.can("puzzles.metadata.any")
        or (series.created_by_id == user.id and user.can("puzzles.metadata.own"))
    )


def puzzle_series_contexts(db: Session, puzzle: Puzzle, user, group=None) -> list[dict]:
    memberships = db.scalars(select(PuzzleSeriesEntry).where(PuzzleSeriesEntry.puzzle_id == puzzle.id)).all()
    contexts = []
    for membership in memberships:
        series = membership.series
        if series.archived_at is not None:
            continue
        ordered = sorted(series.entries, key=lambda item: (item.position, item.id))
        active = [entry for entry in ordered if series_entry_available(entry)]
        completed_by_id = {}
        for entry in active:
            shared = group is not None and series.group_progression == "shared"
            completed_by_id[entry.id] = group_puzzle_complete(group, entry.puzzle) if shared else puzzle_progress_complete(db, entry.puzzle, user)
        previous_complete = True
        stages = []
        for entry in ordered:
            available = entry in active
            scheduled = entry.puzzle is not None and puzzle_is_scheduled(entry.puzzle)
            unlocked = series_admin_bypass(user, series) or (available and (not series.require_previous or previous_complete))
            completed = bool(completed_by_id.get(entry.id))
            stages.append({
                "entry": entry,
                "puzzle": entry.puzzle,
                "available": available,
                "scheduled": scheduled,
                "unlocked": unlocked,
                "completed": completed,
                "current": entry.puzzle_id == puzzle.id,
            })
            if series.require_previous and (available or scheduled):
                previous_complete = previous_complete and available and completed
        current = next((stage for stage in stages if stage["current"]), None)
        contexts.append({
            "series": series,
            "stages": stages,
            "current": current,
            "current_number": next((index + 1 for index, stage in enumerate(stages) if stage["current"]), 0),
            "total": len(stages),
        })
    return contexts


def require_series_access(db: Session, puzzle: Puzzle, user, group=None) -> list[dict]:
    contexts = puzzle_series_contexts(db, puzzle, user, group)
    locked = next((context for context in contexts if context["current"] and not context["current"]["unlocked"]), None)
    if locked is not None:
        prior = next((stage for stage in reversed(locked["stages"][:locked["current_number"] - 1]) if stage["available"]), None)
        title = prior["puzzle"].title if prior is not None else "the previous puzzle"
        raise HTTPException(423, f'Complete "{title}" to unlock this series stage')
    return contexts


def series_group_for_request(request: Request, db: Session, puzzle: Puzzle | None):
    group_id = request.path_params.get("group_id") or request.query_params.get("group", "")
    if not group_id or puzzle is None:
        return None
    from app.models import GroupSolve
    group = db.get(GroupSolve, group_id)
    return group if group is not None and group.puzzle_id == puzzle.id else None


def require_web_series_puzzle(request: Request, db: Session, user, puzzle_id: str) -> Puzzle:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(404, "Puzzle not found")
    require_puzzle_access(request, puzzle)
    require_series_access(db, puzzle, user, series_group_for_request(request, db, puzzle))
    return puzzle


def issue_password_setup_token(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    user.reset_token_hash = hash_secret(token)
    user.reset_token_expires_at = utcnow() + timedelta(hours=RESET_TOKEN_TTL_HOURS)
    user.must_change_password = True
    user.session_version = (user.session_version or 1) + 1
    db.commit()
    return token


def issue_api_token(db: Session, user: User) -> str:
    token = new_api_token()
    user.api_token_hash = hash_secret(token)
    user.api_token_prefix = token[:10]
    user.api_token_last_used_at = None
    db.commit()
    return token


def api_token_from_request(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return request.headers.get("x-puzzary-api-token", "").strip()


def setup_url(token: str, db: Session | None = None) -> str:
    return f"{app_base_url(db)}/password/setup#token={token}"


def write_bootstrap_setup_link(user: User, token: str) -> None:
    path = UPLOAD_DIR.parent / "bootstrap-admin-setup.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "Puzzary initial admin setup\n"
        f"Account: {user.display_name}\n"
        f"Setup link: {setup_url(token)}\n"
        f"Expires: {user.reset_token_expires_at}\n"
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    logging.getLogger("puzzary.bootstrap").warning(
        "The first administrator needs a password. Read the one-time setup link from %s.",
        path,
    )


def clear_bootstrap_setup_link() -> None:
    path = UPLOAD_DIR.parent / "bootstrap-admin-setup.txt"
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def bootstrap_admin_pending(db: Session) -> bool:
    return db.scalar(
        select(User.id).where(
            User.is_admin == True,  # noqa: E712
            User.is_active == True,  # noqa: E712
            User.password_hash.is_(None),
        ).limit(1)
    ) is not None


def login_rate_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{username.strip().lower()}"


def login_limited(request: Request, username: str) -> bool:
    key = login_rate_key(request, username)
    now = time.time()
    attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(key, []) if now - stamp < LOGIN_RATE_LIMIT_WINDOW]
    LOGIN_ATTEMPTS[key] = attempts
    return len(attempts) >= LOGIN_RATE_LIMIT_MAX


def record_login_failure(request: Request, username: str) -> None:
    key = login_rate_key(request, username)
    LOGIN_ATTEMPTS.setdefault(key, []).append(time.time())


def clear_login_failures(request: Request, username: str) -> None:
    LOGIN_ATTEMPTS.pop(login_rate_key(request, username), None)


def registration_rate_key(request: Request, kind: str, identity: str = "") -> str:
    host = request.client.host if request.client else "unknown"
    identity_hash = hash_secret(normalized_identity(identity))[:20] if identity else ""
    return f"{kind}:{host}:{identity_hash}"


def consume_registration_attempt(
    request: Request,
    kind: str,
    identity: str,
    window: int,
    maximum: int,
) -> int:
    key = registration_rate_key(request, kind, identity)
    now = time.monotonic()
    with REGISTRATION_ATTEMPT_LOCK:
        attempts = REGISTRATION_ATTEMPTS.setdefault(key, deque())
        while attempts and now - attempts[0] >= window:
            attempts.popleft()
        if len(attempts) >= maximum:
            return max(1, int(window - (now - attempts[0])))
        attempts.append(now)
    return 0


def registration_csrf_token(request: Request) -> str:
    token = str(request.session.get("registration_csrf", ""))
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["registration_csrf"] = token
    return token


def require_registration_csrf(request: Request, token: str) -> None:
    expected = str(request.session.get("registration_csrf", ""))
    if not expected or not token or not hmac.compare_digest(expected, token):
        raise HTTPException(403, "Registration form expired. Reload the page and try again.")
    request.session["registration_csrf"] = secrets.token_urlsafe(32)


def registration_form_context(request: Request, db: Session, **values) -> dict:
    return {
        "request": request,
        "current_user": current_user(request, db),
        "csrf_token": registration_csrf_token(request),
        "verification_hours": registration_verification_hours(db),
        "verification_required": registration_verification_required(db),
        "error": "",
        "submitted": False,
        "display_name": "",
        "email": "",
        **values,
    }


def validate_registration_input(display_name: str, email: str, password: str) -> tuple[str, str]:
    clean_name = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", display_name)).strip()
    if len(clean_name) < 2 or len(clean_name) > 80 or any(ord(character) < 32 for character in clean_name):
        raise ValueError("Display name must be between 2 and 80 readable characters.")
    clean_email = validate_mailbox(email)
    if len(password) > 256:
        raise ValueError("Password is too long.")
    error = password_error(password)
    if error:
        raise ValueError(error)
    return clean_name, clean_email


def issue_registration_verification(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    user.email_verification_token_hash = hash_secret(token)
    user.email_verification_expires_at = utcnow() + timedelta(hours=registration_verification_hours(db))
    user.email_verification_sent_at = utcnow()
    return token


def request_public_registration(
    request: Request,
    db: Session,
    display_name: str,
    email: str,
    password: str,
) -> None:
    if not public_registration_available(db):
        raise HTTPException(403, "Public registration is disabled.")
    ip_retry = consume_registration_attempt(
        request,
        "create-ip",
        "",
        REGISTRATION_RATE_LIMIT_WINDOW,
        REGISTRATION_IP_RATE_LIMIT_MAX,
    )
    identity_retry = consume_registration_attempt(
        request,
        "create-identity",
        email,
        REGISTRATION_RATE_LIMIT_WINDOW,
        REGISTRATION_RATE_LIMIT_MAX,
    )
    retry = max(ip_retry, identity_retry)
    if retry:
        raise HTTPException(429, "Too many registration attempts. Try again later.", headers={"Retry-After": str(retry)})
    clean_name, clean_email = validate_registration_input(display_name, email, password)
    name_identity = normalized_identity(clean_name)
    existing = db.scalar(select(User.id).where(
        (User.display_name_normalized == name_identity)
        | (User.email_normalized == clean_email)
        | (User.display_name == clean_name)
        | (User.email == clean_email)
    ))
    if existing is not None:
        return
    role = registration_default_role(db)
    now = utcnow()
    verification_required = registration_verification_required(db)
    user = User(
        display_name=clean_name,
        display_name_normalized=name_identity,
        email=clean_email,
        email_normalized=clean_email,
        password_hash=hash_password(password),
        is_admin=False,
        is_active=not verification_required,
        permissions_initialized=True,
        permission_overrides={},
        roles=[role],
        registration_source="self",
        registration_created_at=now,
    )
    token = issue_registration_verification(db, user) if verification_required else ""
    try:
        db.add(user)
        db.flush()
        audit(db, user, "user", user.id, "registration.created", {}, {"role_id": role.id})
        if verification_required:
            send_registration_verification(db, user, token)
            audit(db, user, "user", user.id, "registration.email_sent", {}, {})
        else:
            audit(db, user, "user", user.id, "registration.activated", {}, {})
        db.commit()
    except IntegrityError:
        db.rollback()
    except Exception:
        db.rollback()
        logging.getLogger("puzzary.registration").exception("Could not deliver a public registration verification email.")


def resend_registration_verification(request: Request, db: Session, email: str) -> None:
    if not public_registration_available(db):
        raise HTTPException(403, "Public registration is disabled.")
    if not registration_verification_required(db):
        return
    try:
        clean_email = validate_mailbox(email)
    except ValueError:
        clean_email = normalized_identity(email)[:255]
    ip_retry = consume_registration_attempt(
        request,
        "resend-ip",
        "",
        REGISTRATION_RESEND_WINDOW,
        REGISTRATION_IP_RATE_LIMIT_MAX,
    )
    identity_retry = consume_registration_attempt(
        request,
        "resend-identity",
        clean_email,
        REGISTRATION_RESEND_WINDOW,
        REGISTRATION_RESEND_MAX,
    )
    retry = max(ip_retry, identity_retry)
    if retry:
        raise HTTPException(429, "Too many verification requests. Try again later.", headers={"Retry-After": str(retry)})
    user = db.scalar(select(User).where(
        User.email_normalized == clean_email,
        User.registration_source == "self",
        User.is_active == False,  # noqa: E712
        User.email_verified_at.is_(None),
    ))
    if user is None:
        return
    sent_at = as_utc(user.email_verification_sent_at) if user.email_verification_sent_at else None
    if sent_at and (utcnow() - sent_at).total_seconds() < REGISTRATION_RESEND_MIN_SECONDS:
        return
    token = issue_registration_verification(db, user)
    try:
        send_registration_verification(db, user, token)
        audit(db, user, "user", user.id, "registration.email_resent", {}, {})
        db.commit()
    except Exception:
        db.rollback()
        logging.getLogger("puzzary.registration").exception("Could not resend a registration verification email.")


def find_login_user(db: Session, username: str) -> User | None:
    normalized = username.strip()
    if not normalized:
        return None
    identity = normalized_identity(normalized)
    display_name_user = db.scalar(
        select(User).where(
            (User.display_name == normalized)
            | (User.display_name_normalized == identity)
        )
    )
    if display_name_user is not None:
        return display_name_user
    return db.scalar(select(User).where(User.email_normalized == identity))


def api_user_from_token(request: Request, db: Session) -> User | None:
    token = api_token_from_request(request)
    if not token:
        return None
    user = db.scalar(select(User).where(User.api_token_hash == hash_secret(token), User.is_active == True))  # noqa: E712
    if user is not None:
        user.api_token_last_used_at = utcnow()
        db.commit()
    return user


def require_api_user(request: Request, db: Session) -> User:
    user = api_user_from_token(request, db) or current_user(request, db)
    if user is None:
        puzzle = request_puzzle(request, db)
        user = guest_link_user_for_puzzle(request, db, puzzle.id) if puzzle is not None else None
        if user is None:
            raise HTTPException(status_code=401, detail="API token required")
        require_puzzle_access(request, puzzle)
        authorize_request(request, db, user)
        require_series_access(db, puzzle, user, series_group_for_request(request, db, puzzle))
        return user
    request.state.puzzary_user_id = user.id
    authorize_request(request, db, user)
    route_name = getattr(request.scope.get("route"), "name", "")
    puzzle = request_puzzle(request, db)
    management_bypass = puzzle is not None and route_name in {"api_update_manual_puzzle", "api_export_puzzle"} and can_owned(
        user, "puzzles", "metadata", puzzle.created_by_id
    )
    if puzzle is not None and not management_bypass:
        require_puzzle_access(request, puzzle)
        require_series_access(db, puzzle, user, series_group_for_request(request, db, puzzle))
    return user


def normalized_identity(value: str) -> str:
    return unicodedata.normalize("NFKC", value.strip()).casefold()


def normalized_email(value: str) -> str | None:
    email = normalized_identity(value)
    return email or None


def create_user_record(
    db: Session,
    display_name: str,
    email: str = "",
    is_admin: bool = False,
    password: str = "",
) -> tuple[User, str | None]:
    clean_name = display_name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Display name is required")
    clean_name_identity = normalized_identity(clean_name)
    if db.scalar(select(User).where(
        (User.display_name == clean_name) | (User.display_name_normalized == clean_name_identity)
    )):
        raise HTTPException(status_code=400, detail="That display name is already in use")
    clean_email = normalized_email(email)
    if clean_email and db.scalar(select(User).where(
        (User.email == clean_email) | (User.email_normalized == clean_email)
    )):
        raise HTTPException(status_code=400, detail="That email is already in use")
    user = User(
        display_name=clean_name,
        display_name_normalized=clean_name_identity,
        email=clean_email,
        email_normalized=clean_email,
        email_verified_at=utcnow() if clean_email else None,
        registration_source="admin",
        is_admin=is_admin,
        is_active=True,
    )
    from app.models import PermissionRole
    role = db.scalar(select(PermissionRole).where(PermissionRole.name == ('Admin' if is_admin else 'Player')))
    if role is None:
        initialize_permissions(db)
        role = db.scalar(select(PermissionRole).where(PermissionRole.name == ('Admin' if is_admin else 'Player')))
    user.roles = [role]
    user.permissions_initialized = True
    db.add(user)
    db.flush()
    setup_token = None
    if password.strip():
        error = password_error(password)
        if error:
            raise HTTPException(status_code=400, detail=error)
        user.password_hash = hash_password(password)
        user.must_change_password = True
    else:
        setup_token = issue_password_setup_token(db, user)
        db.refresh(user)
        return user, setup_token
    db.commit()
    db.refresh(user)
    return user, setup_token


def ensure_admin_user(db: Session) -> None:
    admin_user = db.scalar(
        select(User).where(User.is_admin == True, User.is_active == True).order_by(User.created_at, User.id)  # noqa: E712
    )
    if admin_user is None:
        admin_user = db.scalar(select(User).where(User.is_admin == True).order_by(User.created_at, User.id))  # noqa: E712
    if admin_user is None:
        admin_user = db.scalar(select(User).order_by(User.created_at, User.id))
    if admin_user is None:
        display_name = os.getenv("PUZZARY_BOOTSTRAP_ADMIN_NAME", "admin").strip() or "admin"
        admin_user = User(
            display_name=display_name,
            display_name_normalized=normalized_identity(display_name),
            registration_source="bootstrap",
            is_admin=True,
            is_active=True,
        )
        db.add(admin_user)
        db.flush()
    else:
        admin_user.is_admin = True
        admin_user.is_active = True
        if not admin_user.display_name_normalized:
            admin_user.display_name_normalized = normalized_identity(admin_user.display_name)
        if admin_user.email and not admin_user.email_normalized:
            admin_user.email_normalized = normalized_email(admin_user.email)
    if admin_user.password_hash:
        db.commit()
        clear_bootstrap_setup_link()
        return
    token_missing_or_expired = (
        not admin_user.reset_token_hash
        or not admin_user.reset_token_expires_at
        or as_utc(admin_user.reset_token_expires_at) <= utcnow()
    )
    if token_missing_or_expired:
        token = issue_password_setup_token(db, admin_user)
        write_bootstrap_setup_link(admin_user, token)
        return
    db.commit()


def seed_word_banks(db: Session) -> None:
    existing = set(db.scalars(select(WordBank.slug)).all())
    dictionaries = db.scalars(select(PuzzleDictionary).order_by(PuzzleDictionary.name)).all()
    for dictionary in dictionaries:
        if dictionary.slug in existing:
            continue
        db.add(
            bank := WordBank(
                name=dictionary.name,
                slug=dictionary.slug,
                description=dictionary.description,
                payload={"words": dictionary.words},
            )
        )
        sync_word_bank_words(db, bank, dictionary.words)
        existing.add(dictionary.slug)

    for slug, words in THEMES.items():
        if slug in existing:
            continue
        db.add(
            bank := WordBank(
                name=slug.replace("_", " ").title(),
                slug=slug,
                description="Seeded from the original Puzzary word list.",
                payload={"words": words},
            )
        )
        sync_word_bank_words(db, bank, words)
        existing.add(slug)
    for bank in db.scalars(select(WordBank)).all():
        if not bank.word_links:
            sync_word_bank_words(db, bank, word_bank_words(bank))
    db.commit()


def seed_word_definitions(db: Session) -> None:
    changed = False
    for word in db.scalars(select(DictionaryWord).order_by(DictionaryWord.word)).all():
        if word.clue.strip() and not word.definitions:
            definition = DictionaryDefinition(
                definition=word.clue.strip(),
                part_of_speech=word.part_of_speech,
                position=0,
                source="legacy_clue",
            )
            definition.tags = definition_tags_from_form(db, [], word.part_of_speech, word.clue)
            word.definitions.append(definition)
            changed = True
    if changed:
        db.commit()


def ensure_definition_from_clue(db: Session, word: DictionaryWord) -> None:
    if word.clue.strip() and not word.definitions:
        definition = DictionaryDefinition(
            definition=word.clue.strip(),
            part_of_speech=word.part_of_speech,
            position=0,
            source="word_clue",
        )
        definition.tags = definition_tags_from_form(db, [], word.part_of_speech, word.clue)
        word.definitions.append(definition)


def word_bank_options(db: Session, include_archived: bool = False) -> list[WordBank]:
    statement = select(WordBank)
    if not include_archived:
        statement = statement.where(WordBank.archived_at.is_(None))
    return db.scalars(statement.order_by(WordBank.name)).all()


def word_bank_for_slug(db: Session, slug: str) -> WordBank | None:
    return db.scalar(select(WordBank).where(WordBank.slug == slug, WordBank.archived_at.is_(None)))


def word_banks_for_slugs(db: Session, slugs: list[str]) -> list[WordBank]:
    normalized = [slug.strip() for slug in slugs if slug.strip()]
    if not normalized:
        return []
    if "__all__" in normalized:
        return word_bank_options(db)
    banks = db.scalars(
        select(WordBank).where(WordBank.slug.in_(normalized), WordBank.archived_at.is_(None))
    ).all()
    by_slug = {bank.slug: bank for bank in banks}
    return [by_slug[slug] for slug in normalized if slug in by_slug]


def word_banks_for_ids(db: Session, bank_ids: list[int]) -> list[WordBank]:
    cleaned = [int(bank_id) for bank_id in bank_ids if int(bank_id) >= 0]
    if not cleaned:
        return []
    if 0 in cleaned:
        return word_bank_options(db)
    banks = db.scalars(
        select(WordBank).where(WordBank.id.in_(cleaned), WordBank.archived_at.is_(None))
    ).all()
    by_id = {bank.id: bank for bank in banks}
    return [by_id[bank_id] for bank_id in cleaned if bank_id in by_id]


def combined_word_bank_entries(db: Session, banks: list[WordBank], excluded_tag_ids: set[int] | None = None) -> list[dict]:
    entries = []
    seen = set()
    dictionary_words = db.scalars(dictionary_statement()).all() if any(word_bank_mode(bank) == "automatic" for bank in banks) else None
    for bank in banks:
        for entry in word_bank_entries(db, bank, excluded_tag_ids, dictionary_words):
            word = entry.get("word") if isinstance(entry, dict) else None
            definition = entry.get("definition") if isinstance(entry, dict) else None
            key = (getattr(word, "id", None), getattr(definition, "id", None) if definition is not None else None)
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
    return entries


def word_bank_source_theme(banks: list[WordBank], fallback: str = "") -> str:
    if len(banks) == 1:
        return banks[0].slug
    if len(banks) > 1:
        return "all_word_banks"
    return fallback


def tags_for_ids(db: Session, tag_ids: list[int]) -> list[PuzzleTag]:
    if not tag_ids:
        return []
    return db.scalars(select(PuzzleTag).where(PuzzleTag.id.in_(tag_ids))).all()


def normalize_tag_type(value: str) -> str:
    return value.strip() or "General"


def typed_tag_slug(tag_type: str, name: str) -> str:
    normalized_type = normalize_tag_type(tag_type)
    if normalized_type == "General":
        return slugify(name)
    return slugify(f"{normalized_type} {name}")


def get_or_create_tag(db: Session, tag_type: str, name: str) -> PuzzleTag | None:
    tag_name = name.strip()
    if not tag_name:
        return None
    normalized_type = normalize_tag_type(tag_type)
    existing = db.scalar(
        select(PuzzleTag).where(
            PuzzleTag.tag_type == normalized_type,
            PuzzleTag.name == tag_name,
        )
    )
    if existing is not None:
        return existing
    tag = PuzzleTag(
        name=tag_name,
        tag_type=normalized_type,
        slug=unique_slug(db, PuzzleTag, typed_tag_slug(normalized_type, tag_name)),
    )
    db.add(tag)
    db.flush()
    return tag


def definition_tags_from_form(
    db: Session,
    tag_ids: list[int],
    part_of_speech_tag: str = "",
    clue_tag: str = "",
) -> list[PuzzleTag]:
    tags = tags_for_ids(db, tag_ids)
    existing_ids = {tag.id for tag in tags}
    for tag_type, value in (("Part of Speech", part_of_speech_tag),):
        tag = get_or_create_tag(db, tag_type, value)
        if tag is not None and tag.id not in existing_ids:
            tags.append(tag)
            existing_ids.add(tag.id)
    for value in [line.strip() for line in clue_tag.splitlines() if line.strip()]:
        if len(value) > 120:
            continue
        tag = get_or_create_tag(db, "Clue", value)
        if tag is not None and tag.id not in existing_ids:
            tags.append(tag)
            existing_ids.add(tag.id)
    return tags


def blocked_words_for_generation(db: Session, excluded_tag_ids: set[int]) -> list[str]:
    blocked = []
    for word in db.scalars(select(DictionaryWord)).all():
        has_excluded_tag = excluded_tag_ids and any(tag.id in excluded_tag_ids for tag in word.tags)
        if word.banned_at is not None or has_excluded_tag:
            blocked.append(word.word)
    return blocked


def require_admin_user(request: Request, db: Session) -> User | RedirectResponse:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    authorize_request(request, db, user, administrative=True)
    return user


def require_admin_api(request: Request, db: Session) -> User:
    user = require_api_user(request, db)
    authorize_request(request, db, user, administrative=True)
    return user


def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(User, int(user_id))
    if user is None or not user.is_active:
        request.session.clear()
        return None
    if int(request.session.get("session_version", 0)) != int(user.session_version or 1):
        request.session.clear()
        return None
    return user


def require_web_user(request: Request, db: Session) -> User | RedirectResponse:
    user = current_user(request, db)
    if user is None:
        target = safe_return_to(str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse(url=f"/login?return_to={quote(target)}", status_code=303)
    request.state.puzzary_user_id = user.id
    authorize_request(request, db, user)
    route_name = getattr(request.scope.get("route"), "name", "")
    puzzle = request_puzzle(request, db)
    if puzzle is not None and route_name not in {"puzzle_page", "unlock_puzzle"} and not request.url.path.startswith("/admin/"):
        require_puzzle_access(request, puzzle)
    return user


def found_words_for_progress(progress: PuzzleProgress | None) -> list[str]:
    if progress is None:
        return []
    words = progress.progress_payload.get("found_words", [])
    if not isinstance(words, list):
        return []
    return [str(word).upper() for word in words]


def progress_response(puzzle: Puzzle, user: User, progress: PuzzleProgress | None) -> PuzzleProgressResponse:
    progress_payload = progress.progress_payload if progress is not None else {}
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if puzzle_type is None:
        return PuzzleProgressResponse(
            puzzle_id=puzzle.id,
            user_id=user.id,
            found_words=[],
            answers={},
            expected_count=0,
            found_count=0,
            revision=progress_revision(progress),
        )
    found_count, expected_count = puzzle_type.progress_counter(progress_payload, puzzle.solution_payload)
    return PuzzleProgressResponse(
        puzzle_id=puzzle.id,
        user_id=user.id,
        found_words=progress_payload.get("found_words", []) if isinstance(progress_payload.get("found_words", []), list) else [],
        answers=progress_payload.get("answers", {}) if isinstance(progress_payload.get("answers", {}), dict) else {},
        expected_count=expected_count,
        found_count=found_count,
        revision=progress_revision(progress),
    )


def progress_revision(progress: PuzzleProgress | None) -> str:
    if progress is None:
        return "none"
    value = progress.updated_at
    timestamp = ""
    if value is not None:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        timestamp = value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    material = json.dumps(progress.progress_payload or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{timestamp}\n{material}".encode("utf-8")).hexdigest()


def offline_puzzle_version(puzzle: Puzzle) -> str:
    plugin = get_puzzle_type(puzzle.puzzle_type)
    package = LOADED_PACKAGES.get(puzzle.puzzle_type)
    public_payload = {
        "id": puzzle.id,
        "type": puzzle.puzzle_type,
        "title": puzzle.title,
        "prompt": puzzle.prompt,
        "completion_message": puzzle.completion_message,
        "play_payload": public_play_payload(puzzle),
        "publish_at": puzzle.publish_at.isoformat() if puzzle.publish_at else None,
        "archived_at": puzzle.archived_at.isoformat() if puzzle.archived_at else None,
        "module_version": package.version if package else "",
        "play_script": plugin.play_script if plugin else "",
        "offline_progress_fields": list(plugin.offline_progress_fields) if plugin else [],
        "offline_validation": plugin.offline_validation if plugin else "server",
        "offline_hash_mode": plugin.offline_hash_mode if plugin else "exact",
        "offline_disabled": bool((puzzle.puzzle_metadata or {}).get("offline_disabled")),
    }
    return hashlib.sha256(
        json.dumps(public_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def offline_puzzle_eligibility(puzzle: Puzzle, user) -> tuple[bool, str]:
    plugin = get_puzzle_type(puzzle.puzzle_type)
    if getattr(user, "is_guest", False):
        return False, "Guest sessions stay online-only."
    if plugin is None or not plugin.supports_offline:
        return False, "This puzzle module does not support offline play."
    if plugin.offline_validation == "server":
        return False, "This puzzle module has not declared a complete offline checker."
    if (puzzle.puzzle_metadata or {}).get("offline_disabled"):
        return False, "Offline play is disabled for this puzzle."
    if not can_discover(user, puzzle, play=True):
        return False, "Only currently published library puzzles can be downloaded."
    archive_at = puzzle.auto_archive_at
    if archive_at is not None:
        if archive_at.tzinfo is None:
            archive_at = archive_at.replace(tzinfo=timezone.utc)
        if archive_at <= utcnow():
            return False, "Archived puzzles cannot be downloaded."
    if puzzle.access_password_hash:
        return False, "Password-protected puzzles stay online-only."
    return True, ""


def offline_verification(puzzle: Puzzle) -> dict:
    plugin = get_puzzle_type(puzzle.puzzle_type)
    if plugin is None:
        raise HTTPException(409, "Puzzle module unavailable.")
    verification = {"method": plugin.offline_validation}
    if plugin.offline_game_data is not None:
        game_data = plugin.offline_game_data(deepcopy(puzzle.solution_payload or {}))
        if not isinstance(game_data, dict):
            raise HTTPException(409, "The puzzle module returned invalid offline game data.")
        verification["game_data"] = game_data
    if plugin.offline_validation == "local_rules":
        return verification
    if plugin.offline_validation != "solution_hash" or plugin.offline_solution_state is None:
        raise HTTPException(409, "This puzzle does not have a complete offline checker.")
    expected = plugin.offline_solution_state(deepcopy(puzzle.solution_payload or {}))
    if not isinstance(expected, dict):
        raise HTTPException(409, "The puzzle module returned an invalid offline verification state.")
    def commitment(state: dict) -> dict:
        if not isinstance(state, dict):
            raise HTTPException(409, "The puzzle module returned an invalid offline verification state.")
        salt = secrets.token_hex(16)
        canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return {
            "salt": salt,
            "digest": hashlib.sha256(f"{salt}\n{canonical}".encode("utf-8")).hexdigest(),
            "hash_mode": plugin.offline_hash_mode,
        }
    verification.update({
        "algorithm": "SHA-256",
        **commitment(expected),
    })
    if plugin.offline_solution_parts is not None:
        parts = plugin.offline_solution_parts(deepcopy(puzzle.solution_payload or {}))
        if not isinstance(parts, dict) or any(not isinstance(key, str) for key in parts):
            raise HTTPException(409, "The puzzle module returned invalid offline verification parts.")
        verification["parts"] = {key: commitment(value) for key, value in parts.items()}
    return verification


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": "puzzary", "version": __version__}


def qr_svg_bytes(value: str) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=1, box_size=8)
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage, attrib={"class": "puzzary-qr"})
    buffer = BytesIO()
    image.save(buffer)
    return buffer.getvalue()


@app.get("/puzzles/{puzzle_id}/qr.svg")
def puzzle_qr_svg(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    private = puzzle_is_unlisted(puzzle) or not puzzle_is_published(puzzle)
    if private:
        user = current_user(request, db)
        if user is None or not can_view(user, puzzle):
            raise HTTPException(status_code=404, detail="Puzzle not found")
    return Response(
        content=qr_svg_bytes(puzzle_qr_short_url(puzzle.id, db)),
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=3600" if private else "public, max-age=3600"},
    )


@app.get("/q/{puzzle_id}")
def puzzle_qr_redirect(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    if puzzle_is_unlisted(puzzle) or not puzzle_is_published(puzzle):
        user = current_user(request, db)
        if user is None or not can_view(user, puzzle):
            raise HTTPException(status_code=404, detail="Puzzle not found")
    metadata = dict(puzzle.puzzle_metadata or {})
    qr_stats = metadata.get("qr")
    if not isinstance(qr_stats, dict):
        qr_stats = {}
    qr_stats["scan_count"] = int(qr_stats.get("scan_count") or 0) + 1
    qr_stats["last_scanned_at"] = utcnow().isoformat()
    metadata["qr"] = qr_stats
    puzzle.puzzle_metadata = metadata
    db.commit()
    return RedirectResponse(url=f"/puzzles/{puzzle.id}?source=qr", status_code=303)


def guest_share_context(request: Request, db: Session, token: str, error: str = ""):
    link = guest_link_from_token(db, token)
    puzzle = db.get(Puzzle, link.puzzle_id) if link else None
    if link is None or not guest_link_is_valid(link, puzzle):
        raise HTTPException(410, "This guest link has expired or was revoked")
    existing = guest_link_user_for_puzzle(request, db, puzzle.id)
    if existing and existing.guest_link.id == link.id:
        return RedirectResponse(f"/puzzles/{puzzle.id}", status_code=303)
    account = current_user(request, db)
    if account is not None and can_view(account, puzzle, play=True) and puzzle_access_granted(request, puzzle):
        return RedirectResponse(f"/puzzles/{puzzle.id}", status_code=303)
    return templates.TemplateResponse(request, "guest_share_join.html", {
        "request": request, "current_user": account, "link": link,
        "puzzle": puzzle, "token": token, "error": error,
    })


@app.get("/share/{token}", response_class=HTMLResponse)
def open_guest_share(token: str, request: Request, db: Session = Depends(get_db)):
    return guest_share_context(request, db, token)


@app.post("/share/{token}", response_class=HTMLResponse)
def activate_guest_share(
    token: str,
    request: Request,
    display_name: str = Form(""),
    puzzle_password: str = Form(""),
    db: Session = Depends(get_db),
):
    link = guest_link_from_token(db, token, for_update=True)
    puzzle = db.get(Puzzle, link.puzzle_id) if link else None
    if link is None or not guest_link_is_valid(link, puzzle):
        raise HTTPException(410, "This guest link has expired or was revoked")
    name = display_name.strip()
    if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
        return guest_share_context(request, db, token, "Enter a guest display name.")
    try:
        require_puzzle_access(request, puzzle, puzzle_password)
    except HTTPException as error:
        if error.status_code == 429:
            raise
        return guest_share_context(request, db, token, "The puzzle password was not accepted.")
    account = current_user(request, db)
    if account is not None and can_view(account, puzzle, play=True):
        return RedirectResponse(f"/puzzles/{puzzle.id}", status_code=303)
    if link.max_uses and link.use_count >= link.max_uses:
        raise HTTPException(410, "This guest link has reached its use limit")
    active_cutoff = utcnow() - timedelta(minutes=GUEST_LINK_ACTIVE_MINUTES)
    active_count = db.scalar(select(func.count()).select_from(PuzzleGuestSession).where(
        PuzzleGuestSession.link_id == link.id,
        PuzzleGuestSession.revoked_at.is_(None),
        PuzzleGuestSession.last_seen_at >= active_cutoff,
    )) or 0
    if link.max_concurrent_guests and active_count >= link.max_concurrent_guests:
        raise HTTPException(409, "This guest link currently has no available guest slots")
    duplicate = db.scalar(select(PuzzleGuestSession).where(
        PuzzleGuestSession.link_id == link.id,
        func.lower(PuzzleGuestSession.display_name) == name.lower(),
        PuzzleGuestSession.revoked_at.is_(None),
        PuzzleGuestSession.last_seen_at >= active_cutoff,
    ))
    if duplicate is not None:
        return guest_share_context(request, db, token, "That guest name is already active on this link.")
    session_token = secrets.token_urlsafe(32)
    guest_session = PuzzleGuestSession(
        link_id=link.id,
        token_hash=hash_secret(session_token),
        display_name=name,
        progress_payload={},
        last_seen_at=utcnow(),
    )
    db.add(guest_session)
    link.use_count += 1
    link.last_used_at = utcnow()
    db.commit()
    sessions = dict(request.session.get("puzzle_guest_links", {}))
    if len(sessions) >= 20 and puzzle.id not in sessions:
        sessions.pop(next(iter(sessions)))
    sessions[puzzle.id] = session_token
    request.session["puzzle_guest_links"] = sessions
    return RedirectResponse(f"/puzzles/{puzzle.id}", status_code=303)


def begin_oidc_authorization(
    db: Session,
    provider: OIDCProvider,
    *,
    intent: str,
    user_id: int | None,
    return_to: str,
) -> str:
    now = utcnow()
    db.execute(
        delete(OIDCAuthAttempt).where(
            (OIDCAuthAttempt.expires_at < now)
            | (OIDCAuthAttempt.used_at < now - timedelta(days=1))
        )
    )
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    authorization_url = new_authorization_request(
        provider,
        oidc_callback_url(db),
        state,
        nonce,
        code_verifier,
    )
    db.add(
        OIDCAuthAttempt(
            state_hash=hash_secret(state),
            provider_id=provider.id,
            intent=intent,
            user_id=user_id,
            code_verifier_encrypted=encrypt_secret(code_verifier, SESSION_SECRET),
            nonce=nonce,
            return_to=safe_return_to(return_to),
            expires_at=utcnow() + timedelta(minutes=OIDC_ATTEMPT_TTL_MINUTES),
        )
    )
    db.commit()
    return authorization_url


def oidc_failure_redirect(intent: str, message: str) -> RedirectResponse:
    target = "/admin/settings" if intent == "test" else "/account" if intent == "link" else "/login"
    return RedirectResponse(url=f"{target}?error={quote(message[:240])}", status_code=303)


@app.get("/auth/oidc/{provider_id}/start")
def oidc_login_start(
    provider_id: int,
    request: Request,
    return_to: str = "/library",
    db: Session = Depends(get_db),
):
    provider = db.get(OIDCProvider, provider_id)
    if provider is None or not provider.enabled or oidc_login_mode(db) == "local":
        return RedirectResponse(url="/login?error=That%20single%20sign-on%20provider%20is%20not%20available.", status_code=303)
    try:
        url = begin_oidc_authorization(
            db, provider, intent="login", user_id=None, return_to=return_to
        )
    except OIDCError as error:
        return oidc_failure_redirect("login", str(error))
    return RedirectResponse(url=url, status_code=303)


@app.post("/account/oidc/{provider_id}/link")
def oidc_link_start(provider_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    provider = db.get(OIDCProvider, provider_id)
    if provider is None or not provider.enabled:
        return RedirectResponse(url="/account?error=That%20single%20sign-on%20provider%20is%20not%20available.", status_code=303)
    try:
        url = begin_oidc_authorization(
            db, provider, intent="link", user_id=user.id, return_to="/account"
        )
    except OIDCError as error:
        return oidc_failure_redirect("link", str(error))
    return RedirectResponse(url=url, status_code=303)


@app.post("/admin/settings/oidc/{provider_id}/test")
def oidc_test_start(provider_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_permission(user, "settings.manage")
    provider = db.get(OIDCProvider, provider_id)
    if provider is None:
        raise HTTPException(404, "OIDC provider not found")
    try:
        url = begin_oidc_authorization(
            db, provider, intent="test", user_id=user.id, return_to="/admin/settings"
        )
    except OIDCError as error:
        provider.last_test_status = "error"
        provider.last_test_message = str(error)[:300]
        db.commit()
        return oidc_failure_redirect("test", str(error))
    return RedirectResponse(url=url, status_code=303)


@app.get("/auth/oidc/callback")
def oidc_callback(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
    error_description: str = "",
    db: Session = Depends(get_db),
):
    attempt = db.scalar(
        select(OIDCAuthAttempt).where(
            OIDCAuthAttempt.state_hash == hash_secret(state),
            OIDCAuthAttempt.used_at.is_(None),
            OIDCAuthAttempt.expires_at > utcnow(),
        )
    ) if state else None
    if attempt is None:
        return RedirectResponse(url="/login?error=That%20single%20sign-on%20request%20is%20invalid%20or%20expired.", status_code=303)
    attempt.used_at = utcnow()
    db.commit()
    provider = db.get(OIDCProvider, attempt.provider_id)
    if provider is None:
        return oidc_failure_redirect(attempt.intent, "The single sign-on provider no longer exists.")
    if error:
        message = error_description.strip() or "The identity provider denied the sign-in request."
        if attempt.intent == "test":
            provider.last_test_status = "error"
            provider.last_test_message = message[:300]
            db.commit()
        return oidc_failure_redirect(attempt.intent, message)
    if not code:
        return oidc_failure_redirect(attempt.intent, "The identity provider did not return an authorization code.")
    try:
        result = complete_authorization(
            provider,
            decrypt_secret(provider.client_secret_encrypted, SESSION_SECRET),
            oidc_callback_url(db),
            code,
            decrypt_secret(attempt.code_verifier_encrypted, SESSION_SECRET),
            attempt.nonce,
        )
    except OIDCError as oidc_error:
        if attempt.intent == "test":
            provider.last_test_status = "error"
            provider.last_test_message = str(oidc_error)[:300]
            db.commit()
        return oidc_failure_redirect(attempt.intent, str(oidc_error))

    if attempt.intent == "test":
        admin = current_user(request, db)
        if admin is None or admin.id != attempt.user_id or not admin.can("settings.manage"):
            return oidc_failure_redirect("test", "The administrator session used to start this test is no longer active.")
        provider.last_tested_at = utcnow()
        provider.last_test_status = "ok"
        provider.last_test_message = f"Validated issuer and signed ID token for {result.display_name}."[:300]
        audit(db, admin, "oidc_provider", provider.id, "oidc.tested", {}, {"issuer": result.issuer})
        db.commit()
        return RedirectResponse(url="/admin/settings?message=Single%20sign-on%20test%20passed.", status_code=303)

    identity = db.scalar(
        select(OIDCIdentity).where(
            OIDCIdentity.issuer == result.issuer,
            OIDCIdentity.subject == result.subject,
        )
    )
    if attempt.intent == "link":
        user = current_user(request, db)
        if user is None or user.id != attempt.user_id:
            return oidc_failure_redirect("link", "Sign in locally again before linking an identity.")
        if identity is not None and identity.user_id != user.id:
            return oidc_failure_redirect("link", "That external identity is already linked to another account.")
        existing_for_provider = db.scalar(
            select(OIDCIdentity).where(
                OIDCIdentity.provider_id == provider.id,
                OIDCIdentity.user_id == user.id,
            )
        )
        if existing_for_provider is not None and existing_for_provider.subject != result.subject:
            return oidc_failure_redirect("link", "This account is already linked to a different identity from that provider.")
        identity = identity or existing_for_provider or OIDCIdentity(
            provider_id=provider.id,
            user_id=user.id,
            issuer=result.issuer,
            subject=result.subject,
        )
        identity.email = result.email if result.email_verified else None
        identity.display_name = result.display_name
        identity.claims = result.claims
        identity.last_login_at = utcnow()
        db.add(identity)
        audit(db, user, "user", user.id, "oidc.linked", {}, {"provider_id": provider.id, "issuer": result.issuer})
        db.commit()
        return RedirectResponse(url="/account?message=Single%20sign-on%20identity%20linked.", status_code=303)

    if identity is None:
        if provider.provisioning_mode != "jit":
            return oidc_failure_redirect(
                "login",
                "This identity is not linked to a Puzzary account. Sign in locally and link it from Account settings.",
            )
        from app.models import PermissionRole
        role = db.scalar(select(PermissionRole).where(PermissionRole.name == "Player"))
        if role is None:
            initialize_permissions(db)
            role = db.scalar(select(PermissionRole).where(PermissionRole.name == "Player"))
        verified_email = normalized_email(result.email) if result.email_verified else None
        if verified_email and db.scalar(select(User.id).where(
            (User.email == verified_email) | (User.email_normalized == verified_email)
        )) is not None:
            verified_email = None
        oidc_display_name = unique_oidc_display_name(db, result.display_name)
        user = User(
            display_name=oidc_display_name,
            display_name_normalized=normalized_identity(oidc_display_name),
            email=verified_email,
            email_normalized=verified_email,
            email_verified_at=utcnow() if verified_email else None,
            registration_source="oidc",
            is_admin=False,
            is_active=True,
            permissions_initialized=True,
            permission_overrides={},
            roles=[role],
        )
        db.add(user)
        db.flush()
        identity = OIDCIdentity(
            provider_id=provider.id,
            user_id=user.id,
            issuer=result.issuer,
            subject=result.subject,
            email=verified_email,
            display_name=result.display_name,
            claims=result.claims,
            last_login_at=utcnow(),
        )
        db.add(identity)
        audit(db, user, "user", user.id, "oidc.provisioned", {}, {"provider_id": provider.id})
    user = db.get(User, identity.user_id)
    if user is None or not user.is_active:
        return oidc_failure_redirect("login", "That Puzzary account is disabled or unavailable.")
    identity.email = result.email if result.email_verified else identity.email
    identity.display_name = result.display_name
    identity.claims = result.claims
    identity.last_login_at = utcnow()
    user.last_login_at = utcnow()
    db.commit()
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["session_version"] = user.session_version or 1
    request.session["auth_method"] = "oidc"
    request.session["oidc_provider_id"] = provider.id
    return RedirectResponse(url=safe_return_to(attempt.return_to), status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return_to = safe_return_to(request.query_params.get("return_to", ""))
    mode = oidc_login_mode(db)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "current_user": current_user(request, db),
            "error": request.query_params.get("error", ""),
            "message": request.query_params.get("message", ""),
            "return_to": return_to,
            "login_mode": mode,
            "oidc_providers": oidc_enabled_providers(db) if mode != "local" else [],
            "show_local_login": mode != "oidc" or request.query_params.get("local") == "1",
            "bootstrap_admin_pending": bootstrap_admin_pending(db),
            "public_registration_available": public_registration_available(db),
        },
    )


@app.get("/register", response_class=HTMLResponse)
def registration_page(request: Request, db: Session = Depends(get_db)):
    if not public_registration_available(db):
        raise HTTPException(404, "Public registration is not available.")
    cleanup_stale_registrations(db)
    return templates.TemplateResponse(request, "register.html", registration_form_context(request, db))


@app.post("/register", response_class=HTMLResponse)
def registration_submit(
    request: Request,
    csrf_token: str = Form(""),
    display_name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    if not public_registration_available(db):
        raise HTTPException(403, "Public registration is disabled.")
    require_registration_csrf(request, csrf_token)
    if password != confirm_password:
        return templates.TemplateResponse(
            request,
            "register.html",
            registration_form_context(
                request, db, error="Passwords do not match.", display_name=display_name, email=email
            ),
            status_code=400,
        )
    try:
        request_public_registration(request, db, display_name, email, password)
    except ValueError as error:
        return templates.TemplateResponse(
            request,
            "register.html",
            registration_form_context(
                request, db, error=str(error), display_name=display_name, email=email
            ),
            status_code=400,
        )
    except HTTPException as error:
        if error.status_code != 429:
            raise
        return templates.TemplateResponse(
            request,
            "register.html",
            registration_form_context(request, db, error=error.detail),
            status_code=error.status_code,
            headers=error.headers,
        )
    return templates.TemplateResponse(
        request,
        "register.html",
        registration_form_context(request, db, submitted=True),
        status_code=202,
    )


@app.post("/register/resend", response_class=HTMLResponse)
def registration_resend(
    request: Request,
    csrf_token: str = Form(""),
    email: str = Form(""),
    db: Session = Depends(get_db),
):
    if not public_registration_available(db):
        raise HTTPException(403, "Public registration is disabled.")
    require_registration_csrf(request, csrf_token)
    try:
        resend_registration_verification(request, db, email)
    except HTTPException as error:
        if error.status_code != 429:
            raise
        return templates.TemplateResponse(
            request,
            "register.html",
            registration_form_context(request, db, error=error.detail),
            status_code=error.status_code,
            headers=error.headers,
        )
    return templates.TemplateResponse(
        request,
        "register.html",
        registration_form_context(request, db, submitted=True),
        status_code=202,
    )


@app.get("/register/verify", response_class=HTMLResponse)
def registration_verify(request: Request, token: str = "", db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email_verification_token_hash == hash_secret(token))) if token else None
    valid = bool(
        user
        and user.registration_source == "self"
        and not user.is_active
        and user.email_verification_expires_at
        and as_utc(user.email_verification_expires_at) > utcnow()
    )
    if not valid:
        return templates.TemplateResponse(
            request,
            "registration_verified.html",
            {
                "request": request,
                "current_user": current_user(request, db),
                "verified": False,
                "registration_available": public_registration_available(db),
            },
            status_code=400,
        )
    user.is_active = True
    user.email_verified_at = utcnow()
    user.email_verification_token_hash = None
    user.email_verification_expires_at = None
    user.email_verification_sent_at = None
    user.session_version = max(1, user.session_version or 1)
    audit(db, user, "user", user.id, "registration.verified", {}, {})
    db.commit()
    if oidc_login_mode(db) != "oidc":
        request.session.clear()
        request.session["user_id"] = user.id
        request.session["session_version"] = user.session_version
        request.session["auth_method"] = "local"
    return templates.TemplateResponse(
        request,
        "registration_verified.html",
        {
            "request": request,
            "current_user": user if oidc_login_mode(db) != "oidc" else None,
            "verified": True,
            "local_login_available": oidc_login_mode(db) != "oidc",
        },
    )


@app.post("/api/register", status_code=202)
def api_public_registration(
    payload: PublicRegistrationCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    try:
        request_public_registration(request, db, payload.display_name, payload.email, payload.password)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    message = (
        "If the account details can be registered, a verification email will be sent."
        if registration_verification_required(db)
        else "If the account details can be registered, the account is ready to sign in."
    )
    return {"accepted": True, "message": message}


@app.post("/api/register/resend", status_code=202)
def api_public_registration_resend(
    payload: PublicRegistrationResend,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    resend_registration_verification(request, db, payload.email)
    return {"accepted": True, "message": "If an unverified registration exists, a verification email will be sent."}


@app.post("/login")
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    return_to: str = Form("/library"),
    db: Session = Depends(get_db),
):
    safe_target = safe_return_to(return_to)
    login_error_suffix = f"&return_to={quote(safe_target)}" if safe_target != "/library" else ""
    if login_limited(request, username):
        return RedirectResponse(url=f"/login?error=Too%20many%20attempts.%20Try%20again%20soon.{login_error_suffix}", status_code=303)
    user = find_login_user(db, username)
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        record_login_failure(request, username)
        return RedirectResponse(url=f"/login?error=Invalid%20username%20or%20password.{login_error_suffix}", status_code=303)
    if oidc_login_mode(db) == "oidc" and not user.is_admin:
        record_login_failure(request, username)
        return RedirectResponse(url=f"/login?error=Local%20login%20is%20reserved%20for%20administrator%20recovery.{login_error_suffix}", status_code=303)

    request.session.clear()
    request.session["user_id"] = user.id
    request.session["session_version"] = user.session_version or 1
    user.last_login_at = utcnow()
    db.commit()
    clear_login_failures(request, username)
    if user.must_change_password:
        return RedirectResponse(url="/account/password?message=Please%20set%20a%20new%20password.", status_code=303)
    return RedirectResponse(url=safe_target, status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/password/setup", response_class=HTMLResponse)
def password_setup_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    token_hash = hash_secret(token)
    user = db.scalar(select(User).where(User.reset_token_hash == token_hash))
    token_valid = bool(
        user and user.reset_token_expires_at and as_utc(user.reset_token_expires_at) > utcnow()
    )
    return templates.TemplateResponse(
        request,
        "password_setup.html",
        {
            "request": request,
            "current_user": current_user(request, db),
            "token": token,
            "token_valid": token_valid,
            "fragment_setup": not token,
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/password/setup")
def password_setup(
    request: Request,
    token: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    token_hash = hash_secret(token)
    user = db.scalar(select(User).where(User.reset_token_hash == token_hash))
    if user is None or not user.reset_token_expires_at or as_utc(user.reset_token_expires_at) <= utcnow():
        return RedirectResponse(url="/password/setup?error=That%20setup%20link%20is%20invalid%20or%20expired.", status_code=303)
    if password != confirm_password:
        return templates.TemplateResponse(
            request,
            "password_setup.html",
            {
                "request": request,
                "current_user": current_user(request, db),
                "token": token,
                "token_valid": True,
                "fragment_setup": False,
                "error": "Passwords do not match.",
            },
            status_code=400,
        )
    error = password_error(password)
    if error:
        return templates.TemplateResponse(
            request,
            "password_setup.html",
            {
                "request": request,
                "current_user": current_user(request, db),
                "token": token,
                "token_valid": True,
                "fragment_setup": False,
                "error": error,
            },
            status_code=400,
        )
    user.password_hash = hash_password(password)
    user.must_change_password = False
    user.reset_token_hash = None
    user.reset_token_expires_at = None
    user.is_active = True
    user.session_version = (user.session_version or 1) + 1
    user.last_login_at = utcnow()
    db.commit()
    if user.is_admin:
        clear_bootstrap_setup_link()
    request.session["user_id"] = user.id
    request.session["session_version"] = user.session_version
    return RedirectResponse(url="/library", status_code=303)


@app.get("/account/password", response_class=HTMLResponse)
@app.get("/account", response_class=HTMLResponse)
def account_password_page(request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request,
        "account_password.html",
        {
            "request": request,
            "current_user": user,
            "error": request.query_params.get("error", ""),
            "message": request.query_params.get("message", ""),
            "activity": account_activity(db, user),
            "appearance_options": appearance_options(db),
            "appearance_preference": user.appearance_theme or "server",
            "appearance_default": appearance_default_slug(db),
            "oidc_identities": [
                {
                    "identity": identity,
                    "provider": db.get(OIDCProvider, identity.provider_id),
                }
                for identity in db.scalars(
                    select(OIDCIdentity)
                    .where(OIDCIdentity.user_id == user.id)
                    .order_by(OIDCIdentity.created_at)
                )
            ],
            "oidc_link_providers": oidc_enabled_providers(db),
            "oidc_linked_provider_ids": {
                identity.provider_id
                for identity in db.scalars(select(OIDCIdentity).where(OIDCIdentity.user_id == user.id))
            },
        },
    )


@app.get("/api/account/stats")
def account_stats_api(request: Request, db: Session = Depends(get_db)):
    return account_activity(db, require_api_user(request, db))


@app.post("/account/password")
def account_password_update(
    request: Request,
    current_password: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if user.password_hash and not verify_password(current_password, user.password_hash):
        return RedirectResponse(url="/account/password?error=Current%20password%20is%20not%20correct.", status_code=303)
    if password != confirm_password:
        return RedirectResponse(url="/account/password?error=Passwords%20do%20not%20match.", status_code=303)
    error = password_error(password)
    if error:
        return RedirectResponse(url=f"/account/password?error={quote(error)}", status_code=303)
    user.password_hash = hash_password(password)
    user.must_change_password = False
    user.session_version = (user.session_version or 1) + 1
    db.commit()
    request.session["session_version"] = user.session_version
    return RedirectResponse(url="/account/password?message=Password%20updated.", status_code=303)


@app.post("/account/oidc/{identity_id}/unlink")
def account_oidc_unlink(identity_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    identity = db.get(OIDCIdentity, identity_id)
    if identity is None or identity.user_id != user.id:
        raise HTTPException(404, "Linked identity not found")
    identity_count = db.scalar(
        select(func.count()).select_from(OIDCIdentity).where(OIDCIdentity.user_id == user.id)
    ) or 0
    local_login_available = bool(user.password_hash) and (oidc_login_mode(db) != "oidc" or user.is_admin)
    if not local_login_available and identity_count <= 1:
        return RedirectResponse(
            url="/account?error=Add%20another%20sign-in%20method%20before%20removing%20your%20only%20single%20sign-on%20identity.",
            status_code=303,
        )
    audit(db, user, "user", user.id, "oidc.unlinked", {"provider_id": identity.provider_id}, {})
    db.delete(identity)
    db.commit()
    return RedirectResponse(url="/account?message=Single%20sign-on%20identity%20removed.", status_code=303)


@app.post("/account/appearance")
def account_appearance_update(
    request: Request,
    appearance_theme: str = Form("server"),
    db: Session = Depends(get_db),
):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    choices = {item["slug"] for item in appearance_options(db)} | {"server"}
    if appearance_theme not in choices:
        return RedirectResponse(url="/account?error=That%20appearance%20is%20not%20available.", status_code=303)
    user.appearance_theme = "" if appearance_theme == "server" else appearance_theme
    db.commit()
    return RedirectResponse(url="/account?message=Appearance%20updated.", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    return library(request, db)


@app.get("/library", response_class=HTMLResponse)
def library(request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    library_data = filtered_library(db, request, user, include_archived_for_admin=user.can('library.archived_view'))
    active_chips = active_filter_chips(library_data["filters"], library_data["options"])
    for chip in active_chips:
        chip["remove_url"] = query_without(request, chip["param"], chip["value"])
    next_pairs = list(request.query_params.multi_items())
    next_pairs = [(key, value) for key, value in next_pairs if key != "limit"]
    next_pairs.append(("limit", str(library_data["next_limit"])))
    next_url = f"/library?{urlencode(next_pairs, doseq=True)}"
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "request": request,
            "puzzles": library_data["puzzles"],
            "current_user": user,
            "progress_by_puzzle": library_data["progress_by_puzzle"],
            "previews_by_puzzle": library_data["previews_by_puzzle"],
            "labels_by_type": library_data["labels_by_type"],
            "filters": library_data["filters"],
            "filter_options": library_data["options"],
            "active_chips": active_chips,
            "result_count": library_data["result_count"],
            "all_count": library_data["all_count"],
            "has_more": library_data["has_more"],
            "next_url": next_url,
            "library_return_to": quote(str(request.url.path) + (f"?{request.url.query}" if request.url.query else "")),
        },
    )


@app.get("/generate", response_class=HTMLResponse)
def generate_page(request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request,
        "generate.html",
        {
            "request": request,
            "current_user": user,
            "word_banks": word_bank_options(db),
            "puzzle_types": list_enabled_puzzle_types(db),
            "generation_schemas": puzzle_type_generation_schemas(db),
        },
    )


@app.post("/web/puzzles/generate")
async def web_generate_puzzle(
    request: Request,
    puzzle_type: str = Form("word_search"),
    theme: str = Form(""),
    word_bank_slugs: list[str] = Form(default=[]),
    difficulty: str = Form("easy"),
    seed: str = Form(""),
    publish_at: str = Form(""),
    auto_archive_at: str | None = Form(None),
    offline_allowed: bool = Form(True),
    db: Session = Depends(get_db),
):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    puzzle_type_config = get_puzzle_type(puzzle_type)
    if 'auto_archive_at' in form: require_owned(user,'puzzles','schedule',user.id)
    if puzzle_type_config is None or not is_puzzle_type_enabled(db, puzzle_type):
        raise HTTPException(status_code=400, detail="Unsupported puzzle type")
    needs_word_bank = bool(puzzle_type_config and "word_bank" in puzzle_type_config.source_requirements)
    selected_banks = word_banks_for_slugs(db, word_bank_slugs) if needs_word_bank else []
    generation_theme = word_bank_source_theme(selected_banks, theme) if needs_word_bank else (
        theme if (puzzle_type_config is None or puzzle_type_config.theme_mode != "none") else ""
    )
    puzzle = create_puzzle_record(
        PuzzleGenerateRequest(
            puzzle_type=puzzle_type,
            theme=generation_theme,
            word_bank_slugs=[bank.slug for bank in selected_banks],
            difficulty=difficulty,
            seed=seed or None,
            generation_options=generation_options_from_form(form),
            publish_at=parse_local_datetime(publish_at, "Publication date/time") if publish_at.strip() else None,
            offline_allowed=offline_allowed,
        ),
        db,
        creator=user,
        notify=False,
    )
    saved_puzzle = db.get(Puzzle, puzzle.id)
    if saved_puzzle is not None:
        if 'auto_archive_at' in form and not puzzle_is_draft(saved_puzzle):
            archive_at = parse_local_datetime(auto_archive_at or '', "Archive date/time")
            if archive_at and saved_puzzle.publish_at and archive_at <= normalize_publish_at(saved_puzzle.publish_at):
                raise HTTPException(422, "Auto-archive must be after scheduled publication")
            saved_puzzle.auto_archive_at = archive_at
        if puzzle_is_published(saved_puzzle):
            puzzle_notifications.queue_puzzle_publication(db, saved_puzzle)
        db.commit()
    return RedirectResponse(url=f"/puzzles/{puzzle.id}", status_code=303)


@app.post("/web/users")
def web_create_user(request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/admin/users", status_code=303)


def plugin_management_context(db: Session) -> dict:
    status = package_status()
    github_access = plugin_github_access(db)
    installed = []
    for package_data in status["installed"]:
        package = read_manifest(Path(package_data["path"]))
        try:
            configuration = plugin_settings_view(db, package)
            configuration_error = ""
        except ValueError as error:
            configuration = {
                "values": {field["name"]: field.get("default") for field in package.settings},
                "secret_configured": {
                    field["name"]: False for field in package.settings if field["type"] == "secret"
                },
            }
            configuration_error = str(error)
            status["errors"][f"{package.slug}:settings"] = configuration_error
        installed.append({
            **package_data,
            "configuration": configuration,
            "diagnostic": (
                configuration_error
                if configuration_error
                else
                "Restart required to load the installed version."
                if package_data.get("restart_required")
                else "Loaded and available."
                if package_data.get("loaded")
                else "Installed but not loaded; check plugin load errors."
            ),
        })
    status["installed"] = installed
    installed_by_slug = {package["slug"]: package for package in installed}
    rollback_by_slug = {package["slug"]: package for package in status["rollbacks"]}
    official_slugs = {plugin["slug"] for plugin in status["official_catalog"]["plugins"]}
    disabled = disabled_puzzle_type_slugs(db)
    official = []
    for catalog_plugin in status["official_catalog"]["plugins"]:
        slug = catalog_plugin["slug"]
        installed = installed_by_slug.get(slug)
        loaded = LOADED_PACKAGES.get(slug)
        package_type = catalog_plugin.get("package_type", "puzzle")
        registered = loaded is not None if package_type == "content" else get_puzzle_type(slug) is not None
        official.append({
            **catalog_plugin,
            "installed": installed,
            "rollback": rollback_by_slug.get(slug),
            "loaded_version": loaded.version if loaded else None,
            "package_type": package_type,
            "registered": registered,
            "enabled": package_type == "content" or slug not in disabled,
            "source": "GitHub package" if loaded else "Unavailable",
            "restart_required": bool(installed and installed.get("restart_required")),
            "update_available": bool(installed and installed.get("version") != catalog_plugin["version"]),
        })
    return {
        **status,
        "github_access_configured": github_access["configured"],
        "github_access": github_access,
        "official": official,
        "custom_installed": [
            {**package, "enabled": package["slug"] not in disabled}
            for package in status["installed"]
            if package["slug"] not in official_slugs
        ],
        "rollback_by_slug": rollback_by_slug,
        "registered_count": len(list_puzzle_types()),
        "external_count": len(LOADED_PACKAGES),
        "external_slugs": sorted(LOADED_PACKAGES),
    }


def admin_context(
    request: Request,
    db: Session,
    user: User,
    section: str,
    selected_word: DictionaryWord | None = None,
    selected_word_bank: WordBank | None = None,
    selected_tag: PuzzleTag | None = None,
    selected_puzzle: Puzzle | None = None,
    manual_form_error: str = "",
) -> dict:
    puzzles = db.scalars(select(Puzzle).order_by(desc(Puzzle.created_at))).all()
    puzzles = [p for p in puzzles if can_view(user, p) or can_owned(user, 'puzzles', 'edit', p.created_by_id)]
    all_word_banks = db.scalars(select(WordBank).order_by(WordBank.name)).all()
    word_banks = [bank for bank in all_word_banks if bank.archived_at is None]
    categories = db.scalars(select(PuzzleCategory).order_by(PuzzleCategory.name)).all()
    tag_statement = select(PuzzleTag).order_by(PuzzleTag.tag_type, PuzzleTag.name)
    page_value = request.query_params.get("page", "1")[:6]
    list_page = max(1, int(page_value)) if page_value.isdigit() else 1
    tag_search = request.query_params.get("q", "").strip()[:120]
    tag_type_filter = request.query_params.get("type", "").strip()[:80]
    tag_total = 0
    tag_type_counts = {}
    if section == "tags":
        count_statement = select(func.count(PuzzleTag.id))
        tag_type_counts = dict(db.execute(select(PuzzleTag.tag_type, func.count()).group_by(PuzzleTag.tag_type)).all())
        conditions = []
        if tag_search:
            conditions.append(PuzzleTag.name.icontains(tag_search, autoescape=True))
        if tag_type_filter:
            conditions.append(PuzzleTag.tag_type == tag_type_filter)
        tag_total = db.scalar(count_statement.where(*conditions)) or 0
        list_page = min(list_page, max(1, (tag_total + 199) // 200))
        tag_statement = tag_statement.where(*conditions).offset((list_page - 1) * 200).limit(200)
    if section not in {"dashboard", "tags", "tag_detail"}:
        tag_statement = tag_statement.where(PuzzleTag.tag_type != "Clue")
    tags = db.scalars(tag_statement).all()
    tag_groups: dict[str, list[PuzzleTag]] = {}
    tag_usage = tag_usage_counts(db, tags) if section in {"dashboard", "tags", "tag_detail"} else {}
    for tag in tags:
        tag_groups.setdefault(tag.tag_type or "General", []).append(tag)
    activity_events = db.scalars(
        select(PuzzleActivityEvent)
        .options(selectinload(PuzzleActivityEvent.puzzle), selectinload(PuzzleActivityEvent.user))
        .order_by(desc(PuzzleActivityEvent.created_at), desc(PuzzleActivityEvent.id))
        .limit(50)
    ).all()
    attempts = db.scalars(select(PuzzleAttempt).order_by(desc(PuzzleAttempt.created_at)).limit(20)).all()
    progress_rows = db.scalars(select(PuzzleProgress).order_by(desc(PuzzleProgress.updated_at)).limit(20)).all()
    photos = db.scalars(select(PuzzlePhoto).order_by(desc(PuzzlePhoto.created_at)).limit(20)).all()
    if not user.can('activity.view'):
        activity_events, attempts, progress_rows, photos = [], [], [], []
    dictionary_words = []
    dictionary_page = list_page
    dictionary_search = request.query_params.get("q", "").strip()[:80]
    dictionary_total = 0
    needs_bank_dictionary = (
        section == "word_bank_detail"
        and selected_word_bank is not None
        and word_bank_mode(selected_word_bank) == "automatic"
    )
    if section in {"dashboard", "dictionary"} or needs_bank_dictionary:
        statement = dictionary_statement(include_properties=section == "word_bank_detail")
        if section == "dictionary":
            count_statement = select(func.count(DictionaryWord.id))
            if dictionary_search:
                match = DictionaryWord.display_text.icontains(dictionary_search, autoescape=True) | DictionaryWord.word.icontains(dictionary_search, autoescape=True)
                statement = statement.where(match)
                count_statement = count_statement.where(match)
            dictionary_total = db.scalar(count_statement) or 0
            dictionary_page = min(dictionary_page, max(1, (dictionary_total + 99) // 100))
            statement = statement.offset((dictionary_page - 1) * 100).limit(100)
        dictionary_words = db.scalars(statement).all()
    recent_words = db.scalars(select(DictionaryWord).order_by(desc(DictionaryWord.created_at)).limit(8)).all()
    recent_word_banks = db.scalars(select(WordBank).order_by(desc(WordBank.created_at)).limit(8)).all()
    dictionary_import_sources = db.scalars(select(DictionaryImportSource).order_by(DictionaryImportSource.name)).all()
    admin_users = db.scalars(select(User).order_by(User.display_name)).all()
    if not user.can('users.view'):
        admin_users = []
    bank_counts = word_bank_entry_counts(db, all_word_banks) if section in {"dashboard", "word_banks"} else {}
    bank_entries = {selected_word_bank.id: word_bank_entries(db, selected_word_bank, dictionary_words=dictionary_words)} if selected_word_bank is not None else {}
    dashboard_review = dashboard_review_context(db, puzzles, dictionary_words, all_word_banks, tags, bank_counts, tag_usage) if section == "dashboard" else {}
    series = db.scalars(select(PuzzleSeries).order_by(desc(PuzzleSeries.updated_at), PuzzleSeries.title)).all()
    series = [item for item in series if series_admin_bypass(user, item)]
    effective_base_url = app_base_url(db)
    transport_warnings = list(TRANSPORT_CONFIG.warnings)
    if effective_base_url.lower().startswith("https://") and not TRANSPORT_CONFIG.secure_cookies:
        transport_warnings.append(
            "The public URL is HTTPS but session cookies are not Secure. Set APP_BASE_URL to this HTTPS origin or PUZZARY_SECURE_COOKIES=true, then restart Puzzary."
        )
    if (TRANSPORT_CONFIG.native_tls or TRANSPORT_CONFIG.https_redirect) and not effective_base_url.lower().startswith("https://"):
        transport_warnings.append("TLS or HTTPS redirect is enabled, but the saved public base URL still uses HTTP.")
    return {
        "request": request,
        "current_user": user,
        "section": section,
        "now_utc": utcnow(),
        "issues": [i for i in db.scalars(select(PublishedIssue).order_by(desc(PublishedIssue.created_at))) if can_owned(user,'issues','edit',i.created_by_id)] if section == "publishing" else [],
        "series": series,
        "selected_series": None,
        "selected_issue": None,
        "publishing_puzzles": [
            {"id": puzzle.id, "title": puzzle.title, "puzzle_type": puzzle.puzzle_type,
             "archived": puzzle.archived_at is not None}
            for puzzle in puzzles
            if puzzle_is_published(puzzle)
            and not puzzle_is_unlisted(puzzle)
            and not puzzle.access_password_hash
            and (plugin := get_puzzle_type(puzzle.puzzle_type)) is not None
            and plugin.supports_print and not plugin.digital_only
        ] if section == "publishing" else [],
        "app_settings": {
            "public_base_url": effective_base_url,
            "default_base_url": DEFAULT_APP_BASE_URL,
            "oidc_login_mode": oidc_login_mode(db),
            "oidc_callback_url": oidc_callback_url(db),
            "oidc_providers": [
                oidc_provider_payload(provider, db)
                for provider in db.scalars(select(OIDCProvider).order_by(OIDCProvider.display_name, OIDCProvider.id))
            ],
            "registration": registration_settings_view(db),
            "disabled_puzzle_types": sorted(disabled_puzzle_type_slugs(db)),
            "transport": {
                "native_tls": TRANSPORT_CONFIG.native_tls,
                "trusted_proxy_count": len(TRANSPORT_CONFIG.trusted_proxies),
                "https_redirect": TRANSPORT_CONFIG.https_redirect,
                "secure_cookies": TRANSPORT_CONFIG.secure_cookies,
                "warnings": transport_warnings,
            },
        },
        "puzzles": puzzles,
        "live_puzzles": [puzzle for puzzle in puzzles if puzzle.archived_at is None],
        "archived_puzzles": [puzzle for puzzle in puzzles if puzzle.archived_at is not None],
        "all_word_banks": all_word_banks,
        "word_banks": word_banks,
        "word_bank_texts": {selected_word_bank.id: word_bank_words(selected_word_bank)} if selected_word_bank is not None else {},
        "word_bank_entries": bank_entries,
        "word_bank_counts": bank_counts,
        "dictionary_page": dictionary_page,
        "dictionary_search": dictionary_search,
        "dictionary_total": dictionary_total,
        "dictionary_has_more": dictionary_page * 100 < dictionary_total,
        "categories": categories,
        "tags": tags,
        "assignable_tags": [tag for tag in tags if tag.tag_type != "Clue"],
        "tag_groups": tag_groups,
        "tag_usage": tag_usage,
        "tag_page": list_page,
        "tag_search": tag_search,
        "tag_type_filter": tag_type_filter,
        "tag_type_counts": tag_type_counts,
        "tag_total": tag_total,
        "tag_has_more": list_page * 200 < tag_total,
        "attempts": attempts,
        "activity_events": activity_events,
        "progress_rows": progress_rows,
        "photos": photos,
        "dictionary_words": dictionary_words,
        "recent_words": recent_words,
        "recent_word_banks": recent_word_banks,
        "dictionary_import_sources": dictionary_import_sources,
        "admin_users": admin_users,
        "dashboard_review": dashboard_review,
        "selected_word": selected_word,
        "selected_word_bank": selected_word_bank,
        "selected_tag": selected_tag,
        "selected_puzzle": selected_puzzle,
        "manual_form_error": manual_form_error,
        "puzzle_types": list_enabled_puzzle_types(db),
        "all_puzzle_types": list_puzzle_types(),
        "generation_schemas": puzzle_type_generation_schemas(db),
        "plugin_management": plugin_management_context(db) if section == "plugins" else {},
    }


def render_admin(
    request: Request,
    db: Session,
    section: str,
    selected_word: DictionaryWord | None = None,
    selected_word_bank: WordBank | None = None,
    selected_tag: PuzzleTag | None = None,
    selected_puzzle: Puzzle | None = None,
    manual_form_error: str = "",
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request,
        "admin.html",
        admin_context(
            request,
            db,
            user,
            section,
            selected_word,
            selected_word_bank,
            selected_tag,
            selected_puzzle,
            manual_form_error,
        ),
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "dashboard")


@app.get("/admin/puzzles", response_class=HTMLResponse)
def admin_puzzles_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "puzzles")


def managed_series(db: Session, user, series_id: str) -> PuzzleSeries:
    series = db.get(PuzzleSeries, series_id)
    if series is None:
        raise HTTPException(404, "Puzzle series not found")
    if not series_admin_bypass(user, series):
        raise HTTPException(403, "Puzzle metadata permission required")
    return series


def write_series_positions(db: Session, entries: list[PuzzleSeriesEntry]) -> None:
    for position, entry in enumerate(entries, start=1):
        entry.position = -position
    db.flush()
    for position, entry in enumerate(entries, start=1):
        entry.position = position


@app.get("/admin/series", response_class=HTMLResponse)
def admin_series_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "series")


@app.get("/admin/series/{series_id}", response_class=HTMLResponse)
def admin_series_detail(series_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    series = managed_series(db, user, series_id)
    context = admin_context(request, db, user, "series")
    context["selected_series"] = series
    return templates.TemplateResponse(request, "admin.html", context)


@app.post("/admin/series")
def admin_create_series(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    require_previous: bool = Form(False),
    group_progression: str = Form("shared"),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not (user.can("puzzles.metadata.own") or user.can("puzzles.metadata.any")):
        raise HTTPException(403, "Puzzle metadata permission required")
    clean_title = title.strip()
    if not clean_title or len(clean_title) > 160:
        raise HTTPException(422, "Enter a series title of at most 160 characters")
    if group_progression not in {"shared", "individual"}:
        raise HTTPException(422, "Choose shared or individual Group Solve progression")
    series = PuzzleSeries(
        id=uuid.uuid4().hex[:24], title=clean_title, description=description.strip()[:2000],
        require_previous=require_previous, group_progression=group_progression, created_by_id=user.id,
    )
    db.add(series)
    audit(db, user, "puzzle_series", series.id, "series.created", {}, {
        "title": series.title, "require_previous": require_previous, "group_progression": group_progression,
    })
    db.commit()
    return RedirectResponse(f"/admin/series/{series.id}", status_code=303)


@app.post("/admin/series/{series_id}")
def admin_update_series(
    series_id: str,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    require_previous: bool = Form(False),
    group_progression: str = Form("shared"),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    series = managed_series(db, user, series_id)
    clean_title = title.strip()
    if not clean_title or len(clean_title) > 160 or group_progression not in {"shared", "individual"}:
        raise HTTPException(422, "Enter a valid title and Group Solve progression")
    before = {"title": series.title, "require_previous": series.require_previous, "group_progression": series.group_progression}
    series.title = clean_title
    series.description = description.strip()[:2000]
    series.require_previous = require_previous
    series.group_progression = group_progression
    audit(db, user, "puzzle_series", series.id, "series.updated", before, {
        "title": series.title, "require_previous": require_previous, "group_progression": group_progression,
    })
    db.commit()
    return RedirectResponse(f"/admin/series/{series.id}", status_code=303)


@app.post("/admin/series/{series_id}/entries")
def admin_add_series_entry(
    series_id: str,
    request: Request,
    puzzle_id: str = Form(...),
    completion_message: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    series = managed_series(db, user, series_id)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None or puzzle.archived_at is not None or puzzle_publication_state(puzzle) not in {"scheduled", "published"}:
        raise HTTPException(409, "Only scheduled or published active puzzles can be added to a series")
    require_owned(user, "puzzles", "metadata", puzzle.created_by_id)
    if any(entry.puzzle_id == puzzle.id for entry in series.entries):
        raise HTTPException(409, "That puzzle is already in this series")
    entry = PuzzleSeriesEntry(
        series_id=series.id, puzzle_id=puzzle.id,
        position=max((item.position for item in series.entries), default=0) + 1,
        completion_message=completion_message.strip()[:500],
    )
    db.add(entry)
    audit(db, user, "puzzle_series", series.id, "series.entry_added", {}, {"puzzle_id": puzzle.id})
    db.commit()
    return RedirectResponse(f"/admin/series/{series.id}", status_code=303)


@app.post("/admin/series/{series_id}/entries/{entry_id}")
def admin_update_series_entry(
    series_id: str,
    entry_id: int,
    request: Request,
    action: str = Form("save"),
    completion_message: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    series = managed_series(db, user, series_id)
    entry = next((item for item in series.entries if item.id == entry_id), None)
    if entry is None:
        raise HTTPException(404, "Series entry not found")
    ordered = sorted(series.entries, key=lambda item: (item.position, item.id))
    index = ordered.index(entry)
    if action == "remove":
        db.delete(entry)
        ordered.remove(entry)
        db.flush()
    elif action in {"up", "down"}:
        target = index - 1 if action == "up" else index + 1
        if 0 <= target < len(ordered):
            ordered[index], ordered[target] = ordered[target], ordered[index]
    elif action == "save":
        entry.completion_message = completion_message.strip()[:500]
    else:
        raise HTTPException(422, "Unknown series entry action")
    write_series_positions(db, ordered)
    audit(db, user, "puzzle_series", series.id, f"series.entry_{action}", {}, {"puzzle_id": entry.puzzle_id})
    db.commit()
    return RedirectResponse(f"/admin/series/{series.id}", status_code=303)


@app.post("/admin/series/{series_id}/archive")
def admin_archive_series(series_id: str, request: Request, restore: bool = Form(False), db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    series = managed_series(db, user, series_id)
    series.archived_at = None if restore else utcnow()
    audit(db, user, "puzzle_series", series.id, "series.restored" if restore else "series.archived", {}, {})
    db.commit()
    return RedirectResponse("/admin/series", status_code=303)


@app.get("/admin/puzzles/manual", response_class=HTMLResponse)
def admin_manual_puzzle_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "puzzle_edit")


@app.get("/admin/puzzles/{puzzle_id}/edit", response_class=HTMLResponse)
def admin_edit_puzzle_page(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    return render_admin(request, db, "puzzle_edit", selected_puzzle=puzzle)


@app.get("/admin/generate", response_class=HTMLResponse)
def admin_generate_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "generate")


@app.get("/admin/word-banks", response_class=HTMLResponse)
def admin_word_banks_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "word_banks")


@app.get("/admin/word-banks/{bank_id}", response_class=HTMLResponse)
def admin_word_bank_detail_page(bank_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = db.get(WordBank, bank_id)
    if bank is None:
        raise HTTPException(status_code=404, detail="Word bank not found")
    return templates.TemplateResponse(
        request,
        "admin.html",
        admin_context(request, db, user, "word_bank_detail", selected_word_bank=bank),
    )


@app.get("/admin/dictionary", response_class=HTMLResponse)
def admin_dictionary_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "dictionary")


@app.get("/admin/dictionary/{word_id}", response_class=HTMLResponse)
def admin_dictionary_word_page(word_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    word = db.get(DictionaryWord, word_id)
    if word is None:
        raise HTTPException(status_code=404, detail="Word not found")
    return templates.TemplateResponse(
        request,
        "admin.html",
        admin_context(request, db, user, "word_detail", word),
    )


@app.get("/admin/taxonomy", response_class=HTMLResponse)
def admin_taxonomy_page(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse(url="/admin/tags", status_code=303)


@app.get("/admin/tags", response_class=HTMLResponse)
def admin_tags_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "tags")


@app.get("/admin/tags/{tag_id}", response_class=HTMLResponse)
def admin_tag_detail_page(tag_id: int, request: Request, db: Session = Depends(get_db)):
    tag = db.get(PuzzleTag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    return render_admin(request, db, "tag_detail", selected_tag=tag)


@app.get("/admin/activity", response_class=HTMLResponse)
def admin_activity_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "activity")


@app.get("/admin/publishing", response_class=HTMLResponse)
def admin_publishing_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "publishing")


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "users")


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "settings")


@app.get("/admin/plugins", response_class=HTMLResponse)
def admin_plugins_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "plugins")


def install_plugin_for_user(db: Session, user: User, url: str, sha256: str) -> dict:
    require_permission(user, "plugins.manage")
    _target, manifest = install_plugin_release(url, sha256, plugin_root(), plugin_github_token(db))
    audit(db, user, "plugin", str(manifest["slug"]), "plugin.installed", {}, {
        "version": str(manifest.get("version", "")),
        "restart_required": True,
    })
    db.commit()
    return manifest


async def install_uploaded_plugin_for_user(
    db: Session,
    user: User,
    upload: UploadFile,
    sha256: str,
) -> dict:
    require_permission(user, "plugins.manage")
    if not (upload.filename or "").lower().endswith(".zip"):
        raise ValueError("Local plugin package must be a ZIP file.")
    with tempfile.TemporaryDirectory(prefix="puzzary-plugin-upload-") as temporary:
        archive = Path(temporary) / "package.zip"
        total = 0
        with archive.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError("Plugin archive exceeds the 100 MB limit.")
                output.write(chunk)
        _target, manifest = install_plugin_archive(
            archive,
            sha256,
            plugin_root(),
            source_url=f"local upload: {Path(upload.filename or 'package.zip').name}",
        )
    audit(db, user, "plugin", str(manifest["slug"]), "plugin.installed", {}, {
        "version": str(manifest.get("version", "")),
        "source": "local_upload",
        "restart_required": True,
    })
    db.commit()
    return manifest


def installed_plugin_package(slug: str):
    target = plugin_root() / slug
    if not target.is_dir():
        raise ValueError(f"No installed package exists for {slug}.")
    return read_manifest(target)


def save_plugin_configuration_for_user(
    db: Session,
    user: User,
    slug: str,
    submitted: dict,
) -> dict:
    require_permission(user, "plugins.manage")
    package = installed_plugin_package(slug)
    if not package.settings:
        raise ValueError(f"{package.name} does not declare configurable settings.")
    previous = plugin_settings_for_package(db, package)
    values = validate_plugin_settings(package, submitted, previous)
    save_plugin_settings(db, slug, values)
    audit(db, user, "plugin", slug, "plugin.settings.saved", {
        "configured_keys": sorted(key for key, value in previous.items() if value not in ("", None, False)),
    }, {
        "configured_keys": sorted(key for key, value in values.items() if value not in ("", None, False)),
    })
    db.commit()
    return plugin_settings_view(db, package)


def uninstall_plugin_for_user(db: Session, user: User, slug: str) -> dict:
    require_permission(user, "plugins.manage")
    _rollback, manifest = uninstall_plugin_package(slug, plugin_root())
    audit(db, user, "plugin", slug, "plugin.uninstalled", {
        "version": str(manifest.get("version", "")),
    }, {
        "installed": False,
        "settings_preserved": True,
        "data_preserved": True,
        "rollback_available": True,
        "restart_required": True,
    })
    db.commit()
    return manifest


def restore_plugin_for_user(db: Session, user: User, slug: str) -> dict:
    require_permission(user, "plugins.manage")
    _target, manifest = restore_plugin_rollback(slug, plugin_root())
    audit(db, user, "plugin", slug, "plugin.rollback_restored", {}, {
        "version": str(manifest.get("version", "")),
        "restart_required": True,
    })
    db.commit()
    return manifest


def official_plugin_entry(slug: str) -> dict:
    entry = next((plugin for plugin in official_plugin_catalog()["plugins"] if plugin["slug"] == slug), None)
    if entry is None:
        raise ValueError(f"Unknown official plugin: {slug}")
    return entry


@app.post("/admin/plugins/install")
def admin_install_plugin(
    request: Request,
    url: str = Form(""),
    sha256: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        manifest = install_plugin_for_user(db, user, url.strip(), sha256.strip())
    except (ValueError, OSError) as error:
        return RedirectResponse(url=f"/admin/plugins?error={quote(str(error))}", status_code=303)
    message = f"{manifest.get('name', manifest['slug'])} {manifest.get('version', '')} installed. Restart Puzzary to activate it."
    return RedirectResponse(url=f"/admin/plugins?message={quote(message)}", status_code=303)


@app.post("/admin/plugins/install-upload")
async def admin_upload_plugin(
    request: Request,
    package: UploadFile = File(...),
    sha256: str = Form(""),
    trust_package: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not trust_package:
        return RedirectResponse(url="/admin/plugins?error=Confirm%20that%20you%20trust%20the%20package%20source.", status_code=303)
    try:
        manifest = await install_uploaded_plugin_for_user(db, user, package, sha256.strip())
    except (ValueError, OSError) as error:
        return RedirectResponse(url=f"/admin/plugins?error={quote(str(error))}", status_code=303)
    message = f"{manifest.get('name', manifest['slug'])} {manifest.get('version', '')} installed. Restart Puzzary to activate it."
    return RedirectResponse(url=f"/admin/plugins?message={quote(message)}", status_code=303)


@app.post("/admin/plugins/github-access")
def admin_save_plugin_github_access(
    request: Request,
    github_token: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_permission(user, "plugins.manage")
    try:
        save_plugin_github_token(db, user, github_token)
    except ValueError as error:
        return RedirectResponse(url=f"/admin/plugins?error={quote(str(error))}", status_code=303)
    return RedirectResponse(
        url="/admin/plugins?message=Private%20GitHub%20credential%20saved.%20It%20will%20not%20be%20shown%20again.",
        status_code=303,
    )


@app.post("/admin/plugins/github-access/test")
def admin_test_plugin_github_access(request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_permission(user, "plugins.manage")
    try:
        result = check_plugin_github_token(db, user)
    except ValueError as error:
        return RedirectResponse(url=f"/admin/plugins?error={quote(str(error))}", status_code=303)
    message = f"Private GitHub access verified for {result['repository']}."
    return RedirectResponse(url=f"/admin/plugins?message={quote(message)}", status_code=303)


@app.post("/admin/plugins/github-access/remove")
def admin_remove_plugin_github_access(request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_permission(user, "plugins.manage")
    remove_plugin_github_token(db, user)
    message = "Saved private GitHub credential removed."
    if plugin_github_access(db)["environment_fallback"]:
        message += " The server environment fallback remains active."
    return RedirectResponse(url=f"/admin/plugins?message={quote(message)}", status_code=303)


@app.post("/admin/plugins/install-official")
def admin_install_official_plugins(
    request: Request,
    slug: str = Form("all"),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    installed = []
    try:
        catalog = official_plugin_catalog()["plugins"]
        selected = catalog if slug == "all" else [official_plugin_entry(slug)]
        for plugin in selected:
            installed.append(install_plugin_for_user(db, user, plugin["url"], plugin["sha256"]))
    except (ValueError, OSError) as error:
        detail = f"Installed {len(installed)} module(s), then stopped: {error}" if installed else str(error)
        return RedirectResponse(url=f"/admin/plugins?error={quote(detail)}", status_code=303)
    message = f"Installed {len(installed)} official module(s). Restart Puzzary to activate them."
    return RedirectResponse(url=f"/admin/plugins?message={quote(message)}", status_code=303)


@app.post("/admin/plugins/status")
def admin_update_plugin_status(
    request: Request,
    disabled_puzzle_types: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_permission(user, "plugins.manage")
    registered = {puzzle_type.slug for puzzle_type in list_puzzle_types()}
    requested_disabled = set(disabled_puzzle_types)
    if not requested_disabled <= registered:
        return RedirectResponse(url="/admin/plugins?error=Unknown%20module%20selection.", status_code=303)
    previous_disabled = disabled_puzzle_type_slugs(db)
    set_disabled_puzzle_type_slugs(db, sorted(requested_disabled))
    for slug in sorted(previous_disabled ^ requested_disabled):
        enabled = slug not in requested_disabled
        audit(
            db,
            user,
            "plugin",
            slug,
            "plugin.enabled" if enabled else "plugin.disabled",
            {"enabled": not enabled},
            {"enabled": enabled},
        )
    db.commit()
    return RedirectResponse(url="/admin/plugins?message=Module%20status%20saved.", status_code=303)


@app.post("/admin/plugins/{slug}/status")
def admin_update_single_plugin_status(
    slug: str,
    request: Request,
    enabled: bool = Form(...),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_permission(user, "plugins.manage")
    registered = {puzzle_type.slug for puzzle_type in list_puzzle_types()}
    if slug not in registered:
        return RedirectResponse(url="/admin/plugins?error=Unknown%20module.", status_code=303)
    disabled = disabled_puzzle_type_slugs(db)
    before = slug not in disabled
    if enabled:
        disabled.discard(slug)
    else:
        disabled.add(slug)
    set_disabled_puzzle_type_slugs(db, sorted(disabled))
    if before != enabled:
        audit(
            db,
            user,
            "plugin",
            slug,
            "plugin.enabled" if enabled else "plugin.disabled",
            {"enabled": before},
            {"enabled": enabled},
        )
    db.commit()
    state = "enabled" if enabled else "disabled"
    return RedirectResponse(url=f"/admin/plugins?message=Module%20{quote(state)}.", status_code=303)


@app.post("/admin/plugins/{slug}/rollback")
def admin_restore_plugin_rollback(slug: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        manifest = restore_plugin_for_user(db, user, slug)
    except (ValueError, OSError) as error:
        return RedirectResponse(url=f"/admin/plugins?error={quote(str(error))}", status_code=303)
    message = f"Restored {manifest.get('name', slug)} {manifest.get('version', '')}. Restart Puzzary to activate it."
    return RedirectResponse(url=f"/admin/plugins?message={quote(message)}", status_code=303)


@app.post("/admin/plugins/{slug}/settings")
async def admin_save_plugin_settings(slug: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        package = installed_plugin_package(slug)
        form = await request.form()
        submitted = {field["name"]: form.get(field["name"], "") for field in package.settings}
        save_plugin_configuration_for_user(db, user, slug, submitted)
    except (ValueError, OSError) as error:
        return RedirectResponse(url=f"/admin/plugins?error={quote(str(error))}", status_code=303)
    return RedirectResponse(url=f"/admin/plugins?message={quote(package.name + ' settings saved.')}", status_code=303)


@app.post("/admin/plugins/{slug}/uninstall")
def admin_uninstall_plugin(slug: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        manifest = uninstall_plugin_for_user(db, user, slug)
    except (ValueError, OSError) as error:
        return RedirectResponse(url=f"/admin/plugins?error={quote(str(error))}", status_code=303)
    message = (
        f"{manifest.get('name', slug)} uninstalled. Its settings, puzzle data, and a recoverable rollback were preserved. "
        "Restart Puzzary to finish."
    )
    return RedirectResponse(url=f"/admin/plugins?message={quote(message)}", status_code=303)


@app.get("/admin/themes", response_class=HTMLResponse)
def admin_themes_page(request: Request, edit: str = "", db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    themes = list(db.scalars(select(AppearanceTheme).order_by(AppearanceTheme.name, AppearanceTheme.slug)))
    selected = db.get(AppearanceTheme, edit) if edit else None
    return templates.TemplateResponse(request, "admin_themes.html", {
        "request":request, "current_user":user, "themes":themes, "builtins":appearance_options(db)[:3],
        "default_theme":appearance_default_slug(db), "selected_theme":selected,
        "selected_manifest":json.dumps(selected.manifest if selected else {}, indent=2),
        "theme_styles":{theme.slug:appearance_package_style(theme.manifest) for theme in themes},
    })


def save_appearance_theme(db: Session, user: User, manifest: dict) -> AppearanceTheme:
    theme = db.get(AppearanceTheme, manifest["slug"])
    before = theme.manifest if theme else {}
    if theme is None:
        theme = AppearanceTheme(slug=manifest["slug"], name=manifest["name"], manifest=manifest)
        db.add(theme)
    theme.name = manifest["name"]
    theme.version = manifest["version"]
    theme.author = manifest["author"]
    theme.description = manifest["description"]
    theme.manifest = manifest
    theme.enabled = True
    audit(db, user, "appearance_theme", theme.slug, "theme.saved", before, manifest)
    db.commit()
    return theme


@app.post("/admin/themes/create")
def admin_create_theme(
    request: Request,
    manifest_json: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        manifest = decode_package(manifest_json.encode("utf-8"))
        theme = save_appearance_theme(db, user, manifest)
    except ValueError as error:
        return RedirectResponse(url=f"/admin/themes?error={quote(str(error))}", status_code=303)
    return RedirectResponse(url=f"/admin/themes?message={quote(f'{theme.name} saved.')}", status_code=303)


@app.post("/admin/themes/import")
def admin_import_theme(
    request: Request,
    package: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        manifest = decode_package(package.file.read(65537))
        theme = save_appearance_theme(db, user, manifest)
    except ValueError as error:
        return RedirectResponse(url=f"/admin/themes?error={quote(str(error))}", status_code=303)
    return RedirectResponse(url=f"/admin/themes?message={quote(f'{theme.name} installed.')}", status_code=303)


@app.get("/admin/themes/{slug}/export")
def admin_export_theme(slug: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    theme = db.get(AppearanceTheme, slug)
    if theme is None:
        raise HTTPException(404, "Theme not found")
    return Response(
        json.dumps(theme.manifest, indent=2) + "\n",
        media_type="application/json",
        headers={"Content-Disposition":f'attachment; filename="{theme.slug}.puzzary-theme.json"'},
    )


@app.post("/admin/themes/{slug}/toggle")
def admin_toggle_theme(slug: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    theme = db.get(AppearanceTheme, slug)
    if theme is None:
        raise HTTPException(404, "Theme not found")
    theme.enabled = not theme.enabled
    if not theme.enabled and appearance_default_slug(db) == slug:
        set_app_setting(db, "appearance_default", "system")
    audit(db, user, "appearance_theme", slug, "theme.enabled" if theme.enabled else "theme.disabled", {}, {})
    db.commit()
    return RedirectResponse(url="/admin/themes?message=Theme%20status%20updated.", status_code=303)


@app.post("/admin/themes/{slug}/default")
def admin_default_theme(slug: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    valid = slug in BUILTIN_SLUGS or db.scalar(select(AppearanceTheme).where(AppearanceTheme.slug == slug, AppearanceTheme.enabled == True)) is not None  # noqa: E712
    if not valid:
        raise HTTPException(409, "Enable the theme before making it the default")
    set_app_setting(db, "appearance_default", slug)
    audit(db, user, "appearance_theme", slug, "theme.default", {}, {"default":slug})
    db.commit()
    return RedirectResponse(url="/admin/themes?message=Default%20appearance%20updated.", status_code=303)


@app.post("/admin/themes/{slug}/delete")
def admin_delete_theme(slug: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    theme = db.get(AppearanceTheme, slug)
    if theme is None:
        raise HTTPException(404, "Theme not found")
    if appearance_default_slug(db) == slug:
        set_app_setting(db, "appearance_default", "system")
    audit(db, user, "appearance_theme", slug, "theme.deleted", theme.manifest, {})
    db.delete(theme)
    db.commit()
    return RedirectResponse(url="/admin/themes?message=Theme%20removed.%20Affected%20users%20now%20use%20the%20server%20default.", status_code=303)


@app.post("/admin/settings")
def admin_update_settings(
    request: Request,
    public_base_url: str | None = Form(None),
    oidc_login_mode_value: str | None = Form(None, alias="oidc_login_mode"),
    public_registration_enabled_value: str | None = Form(None, alias="public_registration_enabled"),
    public_registration_verification_required_value: str | None = Form(
        None, alias="public_registration_verification_required"
    ),
    public_registration_role_id: str | None = Form(None),
    public_registration_verification_hours: str | None = Form(None),
    registration_smtp_host: str | None = Form(None),
    registration_smtp_port: str | None = Form(None),
    registration_smtp_security: str | None = Form(None),
    registration_smtp_username: str | None = Form(None),
    registration_smtp_password: str = Form(""),
    registration_smtp_from: str | None = Form(None),
    remove_registration_smtp_password: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        base_url = app_base_url(db) if public_base_url is None else normalize_base_url(public_base_url) or DEFAULT_APP_BASE_URL
    except ValueError as error:
        return RedirectResponse(url=f"/admin/settings?error={quote(str(error))}", status_code=303)
    base_url_changed = base_url != app_base_url(db)
    if base_url_changed:
        require_permission(user, 'settings.manage')
        for provider in db.scalars(select(OIDCProvider)):
            provider.enabled = False
            provider.last_tested_at = None
            provider.last_test_status = "untested"
            provider.last_test_message = "Public URL changed; test this provider again with the new callback URL."
        set_app_setting(db, "oidc_login_mode", "local")
    set_app_setting(db, "public_base_url", base_url)
    if oidc_login_mode_value is not None and not base_url_changed:
        require_permission(user, "settings.manage")
        login_mode = oidc_login_mode_value.strip().lower()
        if login_mode not in OIDC_LOGIN_MODES:
            return RedirectResponse(url="/admin/settings?error=Unknown%20login%20mode.", status_code=303)
        registration_will_remain_enabled = (
            public_registration_enabled(db)
            if public_registration_enabled_value is None
            else public_registration_enabled_value.strip().lower() in {"1", "true", "yes", "on"}
        )
        if login_mode == "oidc" and registration_will_remain_enabled:
            return RedirectResponse(
                url="/admin/settings?error=Disable%20public%20local-account%20registration%20before%20using%20single-sign-on-only%20mode.",
                status_code=303,
            )
        if login_mode != "local" and not oidc_enabled_providers(db):
            return RedirectResponse(
                url="/admin/settings?error=Enable%20a%20successfully%20tested%20OIDC%20provider%20before%20turning%20on%20single%20sign-on.",
                status_code=303,
            )
        set_app_setting(db, "oidc_login_mode", login_mode)
        audit(db, user, "settings", "oidc_login_mode", "oidc.login_mode", {}, {"mode": login_mode})
    if public_registration_enabled_value is not None:
        require_permission(user, "settings.manage")
        try:
            save_registration_settings(
                db,
                user,
                enabled=public_registration_enabled_value.strip().lower() in {"1", "true", "yes", "on"},
                verification_required=(public_registration_verification_required_value or "off").strip().lower()
                in {"1", "true", "yes", "on"},
                role_id=int(public_registration_role_id or "0"),
                verification_hours=int(public_registration_verification_hours or "24"),
                smtp_host=registration_smtp_host or "",
                smtp_port=int(registration_smtp_port or "587"),
                smtp_security=(registration_smtp_security or "starttls").strip().lower(),
                smtp_username=registration_smtp_username or "",
                smtp_password=registration_smtp_password,
                smtp_from=registration_smtp_from or "",
                remove_smtp_password=remove_registration_smtp_password == "on",
                target_login_mode=oidc_login_mode(db),
            )
        except (TypeError, ValueError) as error:
            db.rollback()
            return RedirectResponse(url=f"/admin/settings?error={quote(str(error))}", status_code=303)
    db.commit()
    return RedirectResponse(url="/admin/settings?message=Settings%20saved.", status_code=303)


@app.post("/admin/settings/oidc")
def admin_create_oidc_provider(
    request: Request,
    display_name: str = Form(""),
    issuer_url: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    scopes: str = Form("openid profile email"),
    provisioning_mode: str = Form("link_only"),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_permission(user, "settings.manage")
    provider = OIDCProvider(created_by_id=user.id)
    try:
        update_oidc_provider(
            provider,
            display_name=display_name,
            issuer_url=issuer_url,
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
            provisioning_mode=provisioning_mode,
            enabled=False,
        )
        duplicate = db.scalar(select(OIDCProvider.id).where(OIDCProvider.issuer_url == provider.issuer_url))
        if duplicate is not None:
            raise OIDCError("A provider with that issuer already exists.")
    except OIDCError as oidc_error:
        return RedirectResponse(url=f"/admin/settings?error={quote(str(oidc_error))}", status_code=303)
    db.add(provider)
    db.flush()
    audit(db, user, "oidc_provider", provider.id, "oidc.created", {}, {"issuer": provider.issuer_url})
    db.commit()
    return RedirectResponse(url="/admin/settings?message=Provider%20saved.%20Test%20it%20before%20enabling.", status_code=303)


@app.post("/admin/settings/oidc/{provider_id}")
def admin_update_oidc_provider(
    provider_id: int,
    request: Request,
    display_name: str = Form(""),
    issuer_url: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    scopes: str = Form("openid profile email"),
    provisioning_mode: str = Form("link_only"),
    enabled: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_permission(user, "settings.manage")
    provider = db.get(OIDCProvider, provider_id)
    if provider is None:
        raise HTTPException(404, "OIDC provider not found")
    before = oidc_provider_payload(provider, db)
    try:
        update_oidc_provider(
            provider,
            display_name=display_name,
            issuer_url=issuer_url,
            client_id=client_id,
            client_secret=client_secret or None,
            scopes=scopes,
            provisioning_mode=provisioning_mode,
            enabled=enabled == "on",
        )
        duplicate = db.scalar(
            select(OIDCProvider.id).where(
                OIDCProvider.issuer_url == provider.issuer_url,
                OIDCProvider.id != provider.id,
            )
        )
        if duplicate is not None:
            raise OIDCError("A provider with that issuer already exists.")
    except OIDCError as oidc_error:
        db.rollback()
        return RedirectResponse(url=f"/admin/settings?error={quote(str(oidc_error))}", status_code=303)
    restore_local_login_if_no_oidc_provider(db)
    audit(db, user, "oidc_provider", provider.id, "oidc.updated", before, oidc_provider_payload(provider, db))
    db.commit()
    return RedirectResponse(url="/admin/settings?message=Single%20sign-on%20provider%20updated.", status_code=303)


@app.post("/admin/settings/oidc/{provider_id}/delete")
def admin_delete_oidc_provider(provider_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_permission(user, "settings.manage")
    provider = db.get(OIDCProvider, provider_id)
    if provider is None:
        raise HTTPException(404, "OIDC provider not found")
    identity_count = db.scalar(
        select(func.count()).select_from(OIDCIdentity).where(OIDCIdentity.provider_id == provider.id)
    ) or 0
    if provider.enabled or identity_count:
        return RedirectResponse(
            url="/admin/settings?error=Disable%20the%20provider%20and%20remove%20its%20linked%20identities%20before%20deleting%20it.",
            status_code=303,
        )
    audit(db, user, "oidc_provider", provider.id, "oidc.deleted", oidc_provider_payload(provider, db), {})
    db.delete(provider)
    restore_local_login_if_no_oidc_provider(db)
    db.commit()
    return RedirectResponse(url="/admin/settings?message=Single%20sign-on%20provider%20deleted.", status_code=303)


@app.post("/admin/users")
def admin_create_user(
    request: Request,
    display_name: str = Form(""),
    email: str = Form(""),
    is_admin: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if is_admin == 'on': require_permission(user, 'roles.manage')
    new_user, setup_token = create_user_record(db, display_name, email, is_admin == "on", password)
    audit(db, user, 'user', new_user.id, 'user.created', {}, {'display_name':new_user.display_name, 'roles':[r.id for r in new_user.roles]})
    db.commit()
    if setup_token:
        return RedirectResponse(
            url=f"/admin/users#setup_user={quote(new_user.display_name)}&setup_token={quote(setup_token)}",
            status_code=303,
        )
    return RedirectResponse(url="/admin/users?message=User%20created.", status_code=303)


@app.post("/admin/users/{user_id}")
def admin_update_user(
    user_id: int,
    request: Request,
    display_name: str | None = Form(None),
    email: str | None = Form(None),
    is_admin: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    admin = require_admin_user(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    lock_permissions(db)
    if not (admin.can('users.edit') or admin.can('users.disable')):
        raise HTTPException(403,'User editing or account status permission required')
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id != admin.id and user.can('roles.manage'): require_permission(admin,'roles.manage')
    requested_active = user.is_active if is_active is None else is_active == 'on'
    if requested_active != user.is_active: require_permission(admin, 'users.disable')
    if is_admin is not None: require_permission(admin, 'roles.manage')
    before = {'display_name':user.display_name, 'email':user.email, 'active':user.is_active, 'roles':[r.id for r in user.roles]}
    clean_name = user.display_name if display_name is None else display_name.strip()
    if not clean_name:
        return RedirectResponse(url="/admin/users?error=Display%20name%20is%20required.", status_code=303)
    clean_name_identity = normalized_identity(clean_name)
    existing_name = db.scalar(select(User).where(
        ((User.display_name == clean_name) | (User.display_name_normalized == clean_name_identity)),
        User.id != user.id,
    ))
    if existing_name:
        return RedirectResponse(url="/admin/users?error=That%20display%20name%20is%20already%20in%20use.", status_code=303)
    clean_email = user.email if email is None else normalized_email(email)
    if clean_name != user.display_name or clean_email != user.email:
        require_permission(admin,'users.edit')
    if clean_email:
        existing_email = db.scalar(select(User).where(
            ((User.email == clean_email) | (User.email_normalized == clean_email)),
            User.id != user.id,
        ))
        if existing_email:
            return RedirectResponse(url="/admin/users?error=That%20email%20is%20already%20in%20use.", status_code=303)
    user.display_name = clean_name
    user.display_name_normalized = clean_name_identity
    user.email = clean_email
    user.email_normalized = clean_email
    if clean_email != before["email"]:
        user.email_verified_at = utcnow() if clean_email else None
    if is_admin is not None:
        from app.models import PermissionRole
        user.roles = [db.scalar(select(PermissionRole).where(PermissionRole.name == ('Admin' if is_admin == 'on' else 'Player')))]
        user.permission_overrides = {}
        user.is_admin = is_admin == 'on'
    if user.is_active != requested_active:
        user.session_version = (user.session_version or 1) + 1
    user.is_active = requested_active
    protect_last_admin(db)
    audit(db, admin, 'user', user.id, 'user.updated', before, {'display_name':user.display_name, 'email':user.email, 'active':user.is_active, 'roles':[r.id for r in user.roles]})
    db.commit()
    return RedirectResponse(url=f"/admin/users/{user.id}?tab=profile&message=User%20updated.", status_code=303)


@app.post("/admin/users/{user_id}/reset-password")
def admin_reset_user_password(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin = require_admin_user(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    token = issue_password_setup_token(db, user)
    audit(db, admin, 'user', user.id, 'password.reset', {}, {})
    db.commit()
    return RedirectResponse(
        url=f"/admin/users#setup_user={quote(user.display_name)}&setup_token={quote(token)}",
        status_code=303,
    )


@app.post("/admin/users/{user_id}/revoke-sessions")
def admin_revoke_user_sessions(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin = require_admin_user(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.session_version = (user.session_version or 1) + 1
    audit(db, admin, 'user', user.id, 'sessions.revoked', {}, {})
    db.commit()
    return RedirectResponse(url="/admin/users?message=Sessions%20revoked.", status_code=303)


@app.post("/admin/users/{user_id}/oidc/{identity_id}/unlink")
def admin_unlink_oidc_identity(
    user_id: int,
    identity_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    admin = require_admin_user(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    require_permission(admin, "users.edit")
    user = db.get(User, user_id)
    identity = db.get(OIDCIdentity, identity_id)
    if user is None or identity is None or identity.user_id != user.id:
        raise HTTPException(404, "Linked identity not found")
    identity_count = db.scalar(
        select(func.count()).select_from(OIDCIdentity).where(OIDCIdentity.user_id == user.id)
    ) or 0
    local_login_available = bool(user.password_hash) and (oidc_login_mode(db) != "oidc" or user.is_admin)
    if not local_login_available and identity_count <= 1:
        return RedirectResponse(
            url=f"/admin/users/{user.id}?tab=security&error=Add%20another%20sign-in%20method%20before%20removing%20the%20user%27s%20only%20single%20sign-on%20identity.",
            status_code=303,
        )
    audit(db, admin, "user", user.id, "oidc.unlinked", {"provider_id": identity.provider_id}, {})
    db.delete(identity)
    user.session_version = (user.session_version or 1) + 1
    db.commit()
    return RedirectResponse(
        url=f"/admin/users/{user.id}?tab=security&message=Single%20sign-on%20identity%20removed.",
        status_code=303,
    )


@app.post("/admin/puzzles/manual")
async def admin_create_manual_puzzle(
    request: Request,
    puzzle_type: str = Form(...),
    title: str = Form(""),
    theme: str = Form("manual"),
    difficulty: str = Form("manual"),
    completion_message: str = Form(""),
    editor_mode: str = Form("raw"),
    prompt: str = Form(""),
    print_payload: str = Form("{}"),
    play_payload: str = Form("{}"),
    solution_payload: str = Form("{}"),
    metadata: str = Form("{}"),
    hints: str = Form(""),
    draft: str = Form("true"),
    visibility: str = Form("public"),
    access_password: str = Form(""),
    publish_at: str = Form(""),
    offline_allowed: bool = Form(True),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        form = await request.form()
        if editor_mode == "helper":
            payload = build_manual_payload_from_helper(db, puzzle_type, title, theme, difficulty, completion_message, draft, form, visibility, access_password, False, publish_at, offline_allowed)
        else:
            payload = manual_payload_from_form(
                puzzle_type,
                title,
                theme,
                difficulty,
                completion_message,
                prompt,
                print_payload,
                play_payload,
                solution_payload,
                metadata,
                hints,
                draft,
                visibility,
                access_password,
                False,
                publish_at,
                offline_allowed,
            )
        puzzle = write_manual_puzzle(db, payload, user)
    except ValueError as error:
        return render_admin(request, db, "puzzle_edit", manual_form_error=str(error))
    return RedirectResponse(url=f"/admin/puzzles/{puzzle.id}/edit?message=Manual%20puzzle%20saved.", status_code=303)


@app.post("/admin/puzzles/{puzzle_id}/edit")
async def admin_update_manual_puzzle(
    puzzle_id: str,
    request: Request,
    puzzle_type: str = Form(...),
    title: str = Form(""),
    theme: str = Form("manual"),
    difficulty: str = Form("manual"),
    completion_message: str = Form(""),
    editor_mode: str = Form("raw"),
    prompt: str = Form(""),
    print_payload: str = Form("{}"),
    play_payload: str = Form("{}"),
    solution_payload: str = Form("{}"),
    metadata: str = Form("{}"),
    hints: str = Form(""),
    draft: str = Form("true"),
    visibility: str = Form("public"),
    access_password: str = Form(""),
    remove_access_password: bool = Form(False),
    publish_at: str = Form(""),
    offline_allowed: bool = Form(True),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    try:
        form = await request.form()
        if editor_mode == "helper":
            payload = build_manual_payload_from_helper(db, puzzle_type, title, theme, difficulty, completion_message, draft, form, visibility, access_password, remove_access_password, publish_at, offline_allowed)
        else:
            payload = manual_payload_from_form(
                puzzle_type,
                title,
                theme,
                difficulty,
                completion_message,
                prompt,
                print_payload,
                play_payload,
                solution_payload,
                metadata,
                hints,
                draft,
                visibility,
                access_password,
                remove_access_password,
                publish_at,
                offline_allowed,
            )
        puzzle = write_manual_puzzle(db, payload, user, puzzle)
    except ValueError as error:
        return render_admin(request, db, "puzzle_edit", selected_puzzle=puzzle, manual_form_error=str(error))
    return RedirectResponse(url=f"/admin/puzzles/{puzzle.id}/edit?message=Manual%20puzzle%20saved.", status_code=303)


@app.post("/admin/puzzles/{puzzle_id}/publish")
def admin_publish_manual_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    error = manual_puzzle_publish_error(db, puzzle)
    if error:
        return render_admin(request, db, "puzzle_edit", selected_puzzle=puzzle, manual_form_error=error)
    publish_puzzle_now(db, user, puzzle)
    db.commit()
    return RedirectResponse(url=f"/puzzles/{puzzle.id}", status_code=303)


@app.post("/admin/users/{user_id}/api-token")
def admin_issue_user_api_token(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin = require_admin_user(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    token = issue_api_token(db, user)
    audit(db, admin, 'user', user.id, 'api_token.rotated', {}, {})
    db.commit()
    return RedirectResponse(
        url=f"/admin/users?api_user={quote(user.display_name)}&api_token={quote(token)}",
        status_code=303,
    )


@app.post("/admin/puzzles/{puzzle_id}/archive")
def admin_archive_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is not None:
        puzzle.archived_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url="/admin/puzzles", status_code=303)


@app.post("/admin/puzzles/{puzzle_id}/unarchive")
def admin_unarchive_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is not None:
        puzzle.archived_at = None
        if puzzle.auto_archive_at is not None and normalize_publish_at(puzzle.auto_archive_at) <= utcnow():
            puzzle.auto_archive_at = None
        db.commit()
    return RedirectResponse(url="/admin/puzzles", status_code=303)


@app.post("/admin/puzzles/{puzzle_id}/delete")
def admin_delete_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is not None:
        photo_paths = [photo.file_path for photo in puzzle.photos]
        puzzle.tags = []
        db.delete(puzzle)
        db.commit()
        for photo_path in photo_paths:
            delete_photo_file(photo_path)
        remove_empty_photo_directory(puzzle_id)
    return RedirectResponse(url="/admin/puzzles", status_code=303)


@app.post("/admin/activity/photos/{photo_id}/delete")
def admin_delete_photo(photo_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    photo = db.get(PuzzlePhoto, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo submission not found")
    file_path = photo.file_path
    puzzle_id = photo.puzzle_id
    db.delete(photo)
    db.commit()
    delete_photo_file(file_path)
    remove_empty_photo_directory(puzzle_id)
    return RedirectResponse(url="/admin/activity", status_code=303)


@app.post("/admin/puzzles/import")
async def admin_import_puzzles(
    request: Request,
    source_url: str = Form(""),
    source_text: str = Form(""),
    source_api_token: str = Form(""),
    dry_run: bool = Form(True),
    duplicate_policy: str = Form("skip"),
    upload: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    uploaded_text = ""
    if upload is not None and upload.filename:
        uploaded_text = (await upload.read(1_000_000 + 1)).decode("utf-8", errors="replace")
        if len(uploaded_text) > 1_000_000:
            raise HTTPException(status_code=400, detail="Uploaded puzzle file is too large")
    context = admin_context(request, db, user, "puzzles")
    try:
        items = parse_puzzle_import_items(
            source_text=uploaded_text or source_text,
            source_url=source_url,
            api_token=source_api_token,
        )
        result = import_portable_puzzles(
            db,
            items,
            creator=user,
            dry_run=dry_run,
            duplicate_policy=duplicate_policy,
        )
    except ValueError as error:
        result = {
            "dry_run": dry_run,
            "errors": [str(error)],
            "items": [],
            "processed": 0,
            "imported": 0,
            "skipped": 0,
        }
    context["puzzle_import_result"] = result
    return templates.TemplateResponse(request, "admin.html", context)


@app.post("/admin/puzzles/{puzzle_id}/guest-links", response_class=HTMLResponse)
def admin_create_guest_link(
    puzzle_id: str,
    request: Request,
    label: str = Form(""),
    expires_at: str = Form(""),
    max_uses: int = Form(0),
    max_concurrent_guests: int = Form(0),
    allow_group_solve: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(404, "Puzzle not found")
    require_owned(user, "puzzles", "metadata", puzzle.created_by_id)
    if not puzzle_guest_link_eligible(puzzle):
        raise HTTPException(409, "Publish and activate this puzzle before creating a guest link")
    expiry = parse_local_datetime(expires_at)
    if expiry is not None and expiry <= utcnow():
        raise HTTPException(422, "Guest link expiration must be in the future")
    if not 0 <= max_uses <= 100000 or not 0 <= max_concurrent_guests <= 1000:
        raise HTTPException(422, "Guest link limits are out of range")
    token = secrets.token_urlsafe(32)
    link = PuzzleGuestLink(
        id=uuid.uuid4().hex[:24], puzzle_id=puzzle.id, token_hash=hash_secret(token),
        label=label.strip()[:120], created_by_id=user.id, expires_at=expiry,
        max_uses=max_uses, max_concurrent_guests=max_concurrent_guests,
        allow_group_solve=allow_group_solve,
    )
    db.add(link)
    audit(db, user, "puzzle_guest_link", link.id, "guest_link.created", {}, {
        "puzzle_id": puzzle.id, "expires_at": expiry.isoformat() if expiry else None,
        "max_uses": max_uses, "max_concurrent_guests": max_concurrent_guests,
        "allow_group_solve": allow_group_solve,
    })
    db.commit()
    context = admin_context(request, db, user, "puzzles")
    context["new_guest_link"] = {
        "id": link.id, "url": f"{app_base_url(db)}/share/{token}", "label": link.label or puzzle.title,
    }
    return templates.TemplateResponse(request, "admin.html", context, status_code=201)


@app.post("/admin/puzzles/{puzzle_id}/guest-links/{link_id}/revoke")
def admin_revoke_guest_link(puzzle_id: str, link_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    link = db.get(PuzzleGuestLink, link_id)
    if puzzle is None or link is None or link.puzzle_id != puzzle.id:
        raise HTTPException(404, "Guest link not found")
    require_owned(user, "puzzles", "metadata", puzzle.created_by_id)
    if link.revoked_at is None:
        link.revoked_at = utcnow()
        audit(db, user, "puzzle_guest_link", link.id, "guest_link.revoked", {"revoked": False}, {"revoked": True})
        db.commit()
    return RedirectResponse("/admin/puzzles", status_code=303)


@app.post("/admin/puzzles/{puzzle_id}/metadata")
async def admin_update_puzzle_metadata(
    puzzle_id: str,
    request: Request,
    category_id: int = Form(0),
    tag_ids: list[int] = Form(default=[]),
    auto_archive_at: str | None = Form(None),
    publish_at: str | None = Form(None),
    completion_message: str = Form(""),
    visibility: str = Form("public"),
    access_password: str = Form(""),
    remove_access_password: bool = Form(False),
    offline_allowed: bool = Form(True),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    form = await request.form()
    if puzzle is not None:
        require_owned(user, 'puzzles', 'metadata', puzzle.created_by_id)
        puzzle.category_id = category_id or None
        puzzle.tags = db.scalars(select(PuzzleTag).where(PuzzleTag.id.in_(tag_ids))).all() if tag_ids else []
        try:
            puzzle.completion_message = normalize_completion_message(completion_message)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        metadata = dict(puzzle.puzzle_metadata or {})
        metadata['visibility'] = 'unlisted' if visibility == 'unlisted' else 'public'
        metadata['offline_disabled'] = not offline_allowed
        puzzle.puzzle_metadata = metadata
        try:
            update_puzzle_access_password(puzzle, access_password, remove_access_password)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if 'auto_archive_at' in form:
            require_owned(user,'puzzles','schedule',puzzle.created_by_id)
            archive_at = parse_local_datetime(auto_archive_at or '', "Archive date/time")
            if archive_at and puzzle.publish_at and puzzle_is_scheduled(puzzle) and archive_at <= normalize_publish_at(puzzle.publish_at):
                raise HTTPException(422, "Auto-archive must be after scheduled publication")
            puzzle.auto_archive_at = archive_at
        if 'publish_at' in form:
            require_owned(user, 'puzzles', 'schedule', puzzle.created_by_id)
            require_owned(user, 'puzzles', 'publish', puzzle.created_by_id)
            schedule_puzzle(db, user, puzzle, publish_at or '')
        db.commit()
    return RedirectResponse(url="/admin/puzzles", status_code=303)


@app.post("/admin/generate")
async def admin_generate_puzzle(
    request: Request,
    puzzle_type: str = Form("word_search"),
    bank_id: int = Form(0),
    bank_ids: list[int] = Form(default=[]),
    theme: str = Form(""),
    difficulty: str = Form("easy"),
    seed: str = Form(""),
    completion_message: str = Form(""),
    visibility: str = Form("public"),
    access_password: str = Form(""),
    category_id: int = Form(0),
    tag_ids: list[int] = Form(default=[]),
    excluded_tag_ids: list[int] = Form(default=[]),
    auto_archive_at: str | None = Form(None),
    publish_at: str = Form(""),
    offline_allowed: bool = Form(True),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle_type_config = get_puzzle_type(puzzle_type)
    if puzzle_type_config is None or not is_puzzle_type_enabled(db, puzzle_type):
        raise HTTPException(status_code=400, detail="Unsupported puzzle type")
    needs_word_bank = "word_bank" in puzzle_type_config.source_requirements
    selected_bank_ids = bank_ids or ([bank_id] if bank_id else [])
    banks = word_banks_for_ids(db, selected_bank_ids) if needs_word_bank else []
    if needs_word_bank and not banks:
        raise HTTPException(status_code=404, detail="Word bank not found")
    generation_theme = ""
    if banks:
        generation_theme = word_bank_source_theme(banks)
    elif puzzle_type_config.theme_mode != "none":
        generation_theme = slugify(theme) if theme.strip() else puzzle_type_config.label
    form = await request.form()
    if 'auto_archive_at' in form: require_owned(user,'puzzles','schedule',user.id)
    puzzle = create_puzzle_record(
        PuzzleGenerateRequest(
            puzzle_type=puzzle_type,
            theme=generation_theme,
            word_bank_slugs=[bank.slug for bank in banks],
            difficulty=difficulty,
            excluded_tag_ids=excluded_tag_ids,
            seed=seed or None,
            generation_options=generation_options_from_form(form),
            completion_message=normalize_completion_message(completion_message),
            visibility="unlisted" if visibility == "unlisted" else "public",
            access_password=access_password,
            publish_at=parse_local_datetime(publish_at, "Publication date/time") if publish_at.strip() else None,
            offline_allowed=offline_allowed,
        ),
        db,
        creator=user,
        notify=False,
    )
    saved_puzzle = db.get(Puzzle, puzzle.id)
    if saved_puzzle is not None:
        source_category = banks[0].category_id if len(banks) == 1 else None
        source_tags = list(banks[0].tags) if len(banks) == 1 else []
        saved_puzzle.category_id = category_id or source_category
        saved_puzzle.tags = tags_for_ids(db, tag_ids) if tag_ids else source_tags
        if 'auto_archive_at' in form and not puzzle_is_draft(saved_puzzle):
            archive_at = parse_local_datetime(auto_archive_at or '', "Archive date/time")
            if archive_at and saved_puzzle.publish_at and archive_at <= normalize_publish_at(saved_puzzle.publish_at):
                raise HTTPException(422, "Auto-archive must be after scheduled publication")
            saved_puzzle.auto_archive_at = archive_at
        if puzzle_is_published(saved_puzzle):
            puzzle_notifications.queue_puzzle_publication(db, saved_puzzle)
        db.commit()
    return RedirectResponse(url=f"/puzzles/{puzzle.id}", status_code=303)


@app.post("/admin/word-banks")
def admin_create_word_bank(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    mode: str = Form("manual"),
    category_id: int = Form(0),
    tag_ids: list[int] = Form(default=[]),
    description: str = Form(""),
    words: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = WordBank(
        name=name.strip(),
        slug=unique_slug(db, WordBank, slug or name),
        category_id=category_id or None,
        description=description.strip(),
        payload={"mode": mode if mode == "automatic" else "manual", "words": parse_words(words)},
    )
    bank.tags = db.scalars(select(PuzzleTag).where(PuzzleTag.id.in_(tag_ids))).all() if tag_ids else []
    db.add(bank)
    db.flush()
    sync_word_bank_words(db, bank, parse_words(words))
    db.commit()
    db.refresh(bank)
    return RedirectResponse(url=f"/admin/word-banks/{bank.id}", status_code=303)


@app.post("/admin/word-banks/{bank_id}")
def admin_update_word_bank(
    bank_id: int,
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    mode: str = Form("manual"),
    category_id: int = Form(0),
    tag_ids: list[int] = Form(default=[]),
    description: str = Form(""),
    words: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = db.get(WordBank, bank_id)
    if bank is not None:
        bank.name = name.strip()
        bank.slug = unique_slug(db, WordBank, slug or name, bank.id)
        bank.category_id = category_id or None
        bank.description = description.strip()
        parsed_words = parse_words(words)
        selected_mode = mode if mode == "automatic" else "manual"
        bank.payload = {"mode": selected_mode, "words": parsed_words}
        bank.tags = db.scalars(select(PuzzleTag).where(PuzzleTag.id.in_(tag_ids))).all() if tag_ids else []
        if selected_mode == "manual" and parsed_words:
            sync_word_bank_words(db, bank, parsed_words)
        db.commit()
    return RedirectResponse(url=f"/admin/word-banks/{bank_id}", status_code=303)


@app.post("/admin/word-banks/{bank_id}/words")
def admin_add_word_bank_word(
    bank_id: int,
    request: Request,
    word_definition: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        word_text, definition_text = word_definition.split(":", 1)
        word_id = int(word_text)
        definition_id = int(definition_text or 0)
    except ValueError:
        return RedirectResponse(url="/admin/word-banks", status_code=303)
    bank = db.get(WordBank, bank_id)
    word = db.get(DictionaryWord, word_id)
    definition = db.get(DictionaryDefinition, definition_id) if definition_id else None
    if bank is not None and word is not None:
        if definition is not None and definition.word_id != word.id:
            definition = None
        payload = bank.payload if isinstance(bank.payload, dict) else {}
        payload["mode"] = "manual"
        bank.payload = payload
        add_word_bank_link(db, bank, word, definition)
        db.commit()
    return RedirectResponse(url=f"/admin/word-banks/{bank_id}", status_code=303)


@app.post("/admin/word-banks/{bank_id}/words/{link_id}/delete")
def admin_delete_word_bank_word(
    bank_id: int,
    link_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    link = db.get(WordBankWord, link_id)
    if link is not None and link.word_bank_id == bank_id:
        db.delete(link)
        db.commit()
    return RedirectResponse(url=f"/admin/word-banks/{bank_id}", status_code=303)


@app.post("/admin/word-banks/{bank_id}/archive")
def admin_archive_word_bank(bank_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = db.get(WordBank, bank_id)
    if bank is not None:
        bank.archived_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url=f"/admin/word-banks/{bank_id}", status_code=303)


@app.post("/admin/word-banks/{bank_id}/unarchive")
def admin_unarchive_word_bank(bank_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = db.get(WordBank, bank_id)
    if bank is not None:
        bank.archived_at = None
        db.commit()
    return RedirectResponse(url=f"/admin/word-banks/{bank_id}", status_code=303)


@app.post("/admin/word-banks/{bank_id}/delete")
def admin_delete_word_bank(bank_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = db.get(WordBank, bank_id)
    if bank is not None:
        for puzzle in bank.puzzles:
            puzzle.word_bank_id = None
        bank.tags = []
        db.delete(bank)
        db.commit()
    return RedirectResponse(url="/admin/word-banks", status_code=303)


@app.post("/admin/dictionary")
def admin_create_dictionary_word(
    request: Request,
    word: str = Form(...),
    display_text: str = Form(""),
    clue: str = Form(""),
    part_of_speech: str = Form(""),
    tag_ids: list[int] = Form(default=[]),
    properties: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = get_or_create_word(db, word)
    if entry is not None:
        entry.display_text = display_text.strip() or normalize_word(word)
        entry.clue = clue.strip()
        entry.part_of_speech = part_of_speech.strip()
        entry.tags = tags_for_ids(db, tag_ids)
        entry.properties = {"notes": properties.strip()} if properties.strip() else {}
        ensure_definition_from_clue(db, entry)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{entry.id}" if entry is not None else "/admin/dictionary", status_code=303)


@app.post("/admin/dictionary/import")
def admin_import_dictionary(
    request: Request,
    source_url: str = Form(...),
    source_type: str = Form("auto"),
    source_label: str = Form(""),
    source_api_token: str = Form(""),
    dry_run: bool = Form(True),
    tag_ids: list[int] = Form(default=[]),
    max_items: int = Form(500),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    context = admin_context(request, db, user, "dictionary")
    try:
        sources = fetch_dictionary_source(source_url, source_type, source_api_token)
        imported_words = parse_imported_words(sources)
        result = import_dictionary_words(
            db,
            imported_words,
            source_label=source_label or source_url,
            tag_ids=tag_ids,
            dry_run=dry_run,
            max_items=max_items,
        )
        result["source_count"] = len(sources)
        result["source_url"] = source_url
        result["source_type"] = source_type
    except ValueError as error:
        result = {
            "dry_run": dry_run,
            "source_url": source_url,
            "source_type": source_type,
            "errors": [str(error)],
        }
    context["dictionary_import_result"] = result
    return templates.TemplateResponse(request, "admin.html", context)


@app.post("/admin/dictionary/import-sources")
def admin_create_dictionary_import_source(
    request: Request,
    name: str = Form(...),
    source_url: str = Form(...),
    source_type: str = Form("puzzary"),
    source_label: str = Form(""),
    source_api_token: str = Form(""),
    sync_interval_hours: int = Form(24),
    max_items: int = Form(500),
    enabled: bool = Form(True),
    sync_now: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    source = DictionaryImportSource(
        name=name.strip(),
        source_url=source_url.strip(),
        source_type=source_type.strip() or "auto",
        source_label=source_label.strip(),
        source_api_token=source_api_token.strip(),
        sync_interval_hours=max(1, min(sync_interval_hours, 24 * 30)),
        max_items=max(1, min(max_items, 5000)),
        enabled=enabled,
    )
    schedule_next_dictionary_import(source, utcnow())
    db.add(source)
    db.commit()
    db.refresh(source)
    if sync_now:
        run_dictionary_import_source(db, source, dry_run=False)
    return RedirectResponse(url="/admin/dictionary", status_code=303)


@app.post("/admin/dictionary/import-sources/{source_id}/sync")
def admin_sync_dictionary_import_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    source = db.get(DictionaryImportSource, source_id)
    if source is not None:
        run_dictionary_import_source(db, source, dry_run=False)
    return RedirectResponse(url="/admin/dictionary", status_code=303)


@app.post("/admin/dictionary/import-sources/{source_id}/toggle")
def admin_toggle_dictionary_import_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    source = db.get(DictionaryImportSource, source_id)
    if source is not None:
        source.enabled = not source.enabled
        if source.enabled and source.next_sync_at is None:
            schedule_next_dictionary_import(source, utcnow())
        db.commit()
    return RedirectResponse(url="/admin/dictionary", status_code=303)


@app.post("/admin/dictionary/import-sources/{source_id}/delete")
def admin_delete_dictionary_import_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    source = db.get(DictionaryImportSource, source_id)
    if source is not None:
        db.delete(source)
        db.commit()
    return RedirectResponse(url="/admin/dictionary", status_code=303)


@app.post("/admin/dictionary/{word_id}")
def admin_update_dictionary_word(
    word_id: int,
    request: Request,
    display_text: str = Form(""),
    clue: str = Form(""),
    part_of_speech: str = Form(""),
    tag_ids: list[int] = Form(default=[]),
    properties: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryWord, word_id)
    if entry is not None:
        entry.display_text = display_text.strip() or entry.word
        entry.clue = clue.strip()
        entry.part_of_speech = part_of_speech.strip()
        entry.tags = tags_for_ids(db, tag_ids)
        entry.properties = {"notes": properties.strip()} if properties.strip() else {}
        ensure_definition_from_clue(db, entry)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/archive")
def admin_archive_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryWord, word_id)
    if entry is not None:
        entry.archived_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/unarchive")
def admin_unarchive_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryWord, word_id)
    if entry is not None:
        entry.archived_at = None
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/ban")
def admin_ban_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryWord, word_id)
    if entry is not None:
        entry.banned_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/unban")
def admin_unban_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryWord, word_id)
    if entry is not None:
        entry.banned_at = None
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/delete")
def admin_delete_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryWord, word_id)
    if entry is not None:
        linked_banks = [link.word_bank for link in entry.bank_links]
        for bank in linked_banks:
            payload_words = bank.payload.get("words", []) if isinstance(bank.payload, dict) else []
            if isinstance(payload_words, list):
                bank.payload = {"words": [word for word in payload_words if normalize_word(str(word)) != entry.word]}
        for definition in entry.definitions:
            definition.tags = []
        entry.tags = []
        entry.bank_links.clear()
        db.delete(entry)
        db.commit()
    return RedirectResponse(url="/admin/dictionary", status_code=303)


@app.post("/admin/dictionary/{word_id}/definitions")
def admin_create_dictionary_definition(
    word_id: int,
    request: Request,
    definition: str = Form(...),
    part_of_speech: str = Form(""),
    part_of_speech_tag: str = Form(""),
    clue_tag: str = Form(""),
    example: str = Form(""),
    tag_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    word = db.get(DictionaryWord, word_id)
    if word is None:
        raise HTTPException(status_code=404, detail="Word not found")
    next_position = max((item.position for item in word.definitions), default=-1) + 1
    entry = DictionaryDefinition(
        definition=definition.strip(),
        part_of_speech=(part_of_speech_tag or part_of_speech).strip(),
        example=example.strip(),
        position=next_position,
        properties=properties_with_clues({}, clue_tag.splitlines()),
    )
    entry.tags = definition_tags_from_form(db, tag_ids, part_of_speech_tag or part_of_speech, clue_tag)
    word.definitions.append(entry)
    db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/definitions/{definition_id}")
def admin_update_dictionary_definition(
    word_id: int,
    definition_id: int,
    request: Request,
    definition: str = Form(...),
    part_of_speech: str = Form(""),
    part_of_speech_tag: str = Form(""),
    clue_tag: str = Form(""),
    example: str = Form(""),
    position: int = Form(0),
    tag_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryDefinition, definition_id)
    if entry is not None and entry.word_id == word_id:
        entry.definition = definition.strip()
        entry.part_of_speech = (part_of_speech_tag or part_of_speech).strip()
        entry.example = example.strip()
        entry.position = position
        entry.properties = properties_with_clues(entry.properties, clue_tag.splitlines())
        entry.tags = definition_tags_from_form(db, tag_ids, part_of_speech_tag or part_of_speech, clue_tag)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/definitions/{definition_id}/delete")
def admin_delete_dictionary_definition(
    word_id: int,
    definition_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryDefinition, definition_id)
    if entry is not None and entry.word_id == word_id:
        entry.tags = []
        db.delete(entry)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/definitions/{definition_id}/archive")
def admin_archive_dictionary_definition(
    word_id: int,
    definition_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryDefinition, definition_id)
    if entry is not None and entry.word_id == word_id:
        entry.archived_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/definitions/{definition_id}/unarchive")
def admin_unarchive_dictionary_definition(
    word_id: int,
    definition_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryDefinition, definition_id)
    if entry is not None and entry.word_id == word_id:
        entry.archived_at = None
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionaries")
def admin_create_dictionary(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    words: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    dictionary = PuzzleDictionary(
        name=name.strip(),
        slug=unique_slug(db, PuzzleDictionary, slug or name),
        description=description.strip(),
        words=parse_words(words),
    )
    db.add(dictionary)
    db.commit()
    return RedirectResponse(url="/admin/word-banks", status_code=303)


@app.post("/admin/dictionaries/{dictionary_id}")
def admin_update_dictionary(
    dictionary_id: int,
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    words: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    dictionary = db.get(PuzzleDictionary, dictionary_id)
    if dictionary is not None:
        dictionary.name = name.strip()
        dictionary.slug = unique_slug(db, PuzzleDictionary, slug or name, dictionary.id)
        dictionary.description = description.strip()
        dictionary.words = parse_words(words)
        db.commit()
    return RedirectResponse(url="/admin/word-banks", status_code=303)


@app.post("/admin/dictionaries/{dictionary_id}/delete")
def admin_delete_dictionary(dictionary_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    dictionary = db.get(PuzzleDictionary, dictionary_id)
    if dictionary is not None:
        db.delete(dictionary)
        db.commit()
    return RedirectResponse(url="/admin/word-banks", status_code=303)


@app.post("/admin/categories")
def admin_create_category(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    db.add(PuzzleCategory(name=name.strip(), slug=unique_slug(db, PuzzleCategory, slug or name), description=description.strip()))
    db.commit()
    return RedirectResponse(url="/admin/tags", status_code=303)


@app.post("/admin/categories/{category_id}")
def admin_update_category(
    category_id: int,
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    category = db.get(PuzzleCategory, category_id)
    if category is not None:
        category.name = name.strip()
        category.slug = unique_slug(db, PuzzleCategory, slug or name, category.id)
        category.description = description.strip()
        db.commit()
    return RedirectResponse(url="/admin/tags", status_code=303)


@app.post("/admin/categories/{category_id}/delete")
def admin_delete_category(category_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    category = db.get(PuzzleCategory, category_id)
    if category is not None:
        for puzzle in category.puzzles:
            puzzle.category_id = None
        for bank in category.word_banks:
            bank.category_id = None
        db.delete(category)
        db.commit()
    return RedirectResponse(url="/admin/tags", status_code=303)


@app.post("/admin/tags")
def admin_create_tag(
    request: Request,
    name: str = Form(...),
    tag_type: str = Form("General"),
    slug: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    normalized_type = normalize_tag_type(tag_type)
    tag = PuzzleTag(
        name=name.strip(),
        tag_type=normalized_type,
        slug=unique_slug(db, PuzzleTag, slug or typed_tag_slug(normalized_type, name)),
    )
    db.add(tag)
    db.commit()
    return RedirectResponse(url=f"/admin/tags/{tag.id}", status_code=303)


@app.post("/admin/tags/{tag_id}")
def admin_update_tag(
    tag_id: int,
    request: Request,
    name: str = Form(...),
    tag_type: str = Form("General"),
    slug: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    tag = db.get(PuzzleTag, tag_id)
    if tag is not None:
        normalized_type = normalize_tag_type(tag_type)
        tag.name = name.strip()
        tag.tag_type = normalized_type
        tag.slug = unique_slug(db, PuzzleTag, slug or typed_tag_slug(normalized_type, name), tag.id)
        db.commit()
    return RedirectResponse(url=f"/admin/tags/{tag_id}", status_code=303)


@app.post("/admin/tags/{tag_id}/delete")
def admin_delete_tag(tag_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    tag = db.get(PuzzleTag, tag_id)
    if tag is not None:
        tag.puzzles = []
        tag.word_banks = []
        tag.dictionary_words = []
        tag.dictionary_definitions = []
        db.delete(tag)
        db.commit()
    return RedirectResponse(url="/admin/tags", status_code=303)


@app.get('/groups', response_class=HTMLResponse)
def group_lobby_page(request: Request, q: str = '', puzzle_type: str = '', available: str = 'any',
                     password: str = 'any', sort: str = 'recent', db: Session = Depends(get_db)):
    from app.groups import public_lobby_items
    lobby = public_lobby_items(db, q, puzzle_type, available, password, sort)
    response = templates.TemplateResponse(request, 'group_lobby.html', {
        'request': request,
        'current_user': current_user(request, db),
        'groups': lobby['groups'],
        'filters': lobby['filters'],
        'puzzle_types': [{'slug': item.slug, 'label': item.label} for item in list_puzzle_types() if item.collaboration],
    })
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/groups/{group_id}')
def group_invite(group_id: str, db: Session = Depends(get_db)):
    from app.groups import group_for
    group = group_for(db, group_id)
    return RedirectResponse(f'/puzzles/{group.puzzle_id}?group={group.id}', status_code=303)


@app.get("/offline", response_class=HTMLResponse)
def offline_library(request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(request, "offline_library.html", {
        "request": request,
        "current_user": user,
    })


def offline_play_context(request: Request, db: Session, puzzle: Puzzle, user) -> dict:
    progress = progress_record_for_user(db, puzzle, user)
    progress_payload = progress.progress_payload if progress is not None else {}
    progress_answers = progress_payload.get("answers", {})
    if not isinstance(progress_answers, dict):
        progress_answers = {}
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    puzzle_template_name = f"puzzle_types/{puzzle.puzzle_type}.html"
    return {
        "request": request,
        "puzzle": puzzle,
        "play_payload": public_play_payload(puzzle),
        "current_user": user,
        "progress_found_words": found_words_for_progress(progress),
        "progress_payload": progress_payload,
        "progress_answers": progress_answers,
        "group_id": "",
        "puzzle_type_definition": puzzle_type,
        "puzzle_template": puzzle_template_name,
        "has_puzzle_template": template_exists(puzzle_template_name),
        "hide_app_chrome": True,
        "offline_copy": True,
        "offline_verification": offline_verification(puzzle),
    }


@app.get("/offline/puzzles/{puzzle_id}", response_class=HTMLResponse)
def offline_puzzle_page(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(404, "Puzzle not found")
    eligible, reason = offline_puzzle_eligibility(puzzle, user)
    if not eligible:
        raise HTTPException(409, reason)
    require_series_access(db, puzzle, user, series_group_for_request(request, db, puzzle))
    response = templates.TemplateResponse(
        request, "offline_puzzle.html", offline_play_context(request, db, puzzle, user)
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.get("/puzzles/{puzzle_id}", response_class=HTMLResponse)
def puzzle_page(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    group_id = request.query_params.get('group', '')
    user = current_user(request, db)
    if not user and group_id:
        from app.groups import group_for, guests_allowed, require_group_user, member_for, writable
        group = group_for(db, group_id)
        if group.puzzle_id != puzzle_id:
            return RedirectResponse(f'/groups/{group.id}', status_code=303)
        guest_puzzle = db.get(Puzzle, group.puzzle_id)
        if not puzzle_is_published(guest_puzzle):
            raise HTTPException(404, 'Puzzle not found')
        if guests_allowed(db, group):
            try:
                user = require_group_user(request, db, group)
            except HTTPException as error:
                if error.status_code not in {401, 423}:
                    raise
                writable(group)
                return templates.TemplateResponse(request, 'group_guest_join.html', {
                    'request': request, 'group': group, 'password_protected': bool(group.password_hash),
                    'puzzle_password_protected': bool(guest_puzzle and guest_puzzle.access_password_hash),
                    'current_user': None,
                })
            member = member_for(db, group, user, active=False)
            if member.status == 'removed':
                raise HTTPException(403, 'You were removed from this session')
    if not user and not group_id:
        user = guest_link_user_for_puzzle(request, db, puzzle_id)
    if not user:
        user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    is_guest = getattr(user, 'is_guest', False)
    if is_guest and not puzzle_is_published(puzzle):
        raise HTTPException(status_code=403, detail="Puzzle access denied")
    if not is_guest and not can_view(user, puzzle, play=True):
        raise HTTPException(status_code=403, detail="Puzzle access denied")
    if not is_guest:
        authorize_request(request, db, user)
    if not puzzle_access_granted(request, puzzle):
        return templates.TemplateResponse(request, "puzzle_unlock.html", {
            "request": request,
            "puzzle": puzzle,
            "current_user": user,
            "return_to": str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
            "error": "",
        }, status_code=423)
    series_group = series_group_for_request(request, db, puzzle)
    try:
        series_contexts = require_series_access(db, puzzle, user, series_group)
    except HTTPException as error:
        if error.status_code != 423:
            raise
        return templates.TemplateResponse(request, "series_locked.html", {
            "request": request,
            "current_user": user,
            "puzzle": puzzle,
            "series_contexts": puzzle_series_contexts(db, puzzle, user, series_group),
            "error": error.detail,
        }, status_code=423)
    users = [] if is_guest else db.scalars(select(User).order_by(User.display_name)).all()
    attempts = [] if is_guest else db.scalars(
        select(PuzzleAttempt).where(PuzzleAttempt.puzzle_id == puzzle_id).order_by(desc(PuzzleAttempt.created_at)).limit(10)
    ).all()
    photos = [] if is_guest else db.scalars(
        select(PuzzlePhoto).where(PuzzlePhoto.puzzle_id == puzzle_id).order_by(desc(PuzzlePhoto.created_at)).limit(10)
    ).all()
    if not is_guest and not user.can('activity.view'):
        attempts = [a for a in attempts if a.user_id == user.id]
        photos = [p for p in photos if p.user_id == user.id]
    progress = progress_record_for_user(db, puzzle, user)
    progress_payload = progress.progress_payload if progress is not None else {}
    progress_answers = progress_payload.get("answers", {})
    if not isinstance(progress_answers, dict):
        progress_answers = {}
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if group_id:
        from app.groups import group_for
        group = group_for(db, group_id)
        if group.puzzle_id != puzzle_id:
            return RedirectResponse(f'/groups/{group.id}', status_code=303)
        progress_payload = {}
        progress_answers = {}
    puzzle_template_name = f"puzzle_types/{puzzle.puzzle_type}.html"
    solution_template_name = f"puzzle_solutions/{puzzle.puzzle_type}.html"
    has_puzzle_template = template_exists(puzzle_template_name)
    has_solution_template = template_exists(solution_template_name)
    offline_eligible, offline_reason = offline_puzzle_eligibility(puzzle, user)
    if group_id:
        offline_eligible = False
        offline_reason = "Group Solve stays online-only."
    return_to = safe_library_return_to(request.query_params.get("return_to", "/library"))
    return templates.TemplateResponse(
        request,
        "puzzle.html",
        {
            "request": request,
            "puzzle": puzzle,
            "play_payload": public_play_payload(puzzle),
            "users": users,
            "attempts": attempts,
            "photos": photos,
            "current_user": user,
            "progress_found_words": found_words_for_progress(progress),
            "progress_payload": progress_payload,
            "progress_answers": progress_answers,
            "group_id": group_id,
            "publication_state": puzzle_publication_state(puzzle),
            "puzzle_type_definition": puzzle_type,
            "puzzle_template": puzzle_template_name,
            "solution_template": solution_template_name,
            "has_puzzle_template": has_puzzle_template,
            "has_solution_template": has_solution_template,
            "playable_puzzle_type": puzzle_type is not None and puzzle_type.supports_web_play and has_puzzle_template,
            "return_to": return_to,
            "series_contexts": series_contexts,
            "series_completion_message": next((
                context["current"]["entry"].completion_message for context in series_contexts
                if context["current"] and context["current"]["entry"].completion_message
            ), ""),
            "offline_eligible": offline_eligible,
            "offline_reason": offline_reason,
        },
    )


@app.post("/puzzles/{puzzle_id}/unlock", response_class=HTMLResponse)
def unlock_puzzle(
    puzzle_id: str,
    request: Request,
    password: str = Form(""),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = current_user(request, db) or guest_link_user_for_puzzle(request, db, puzzle_id)
    if user is None:
        return RedirectResponse(url=f"/login?return_to={quote('/puzzles/' + puzzle_id)}", status_code=303)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None or (not getattr(user, "is_guest_link", False) and not can_view(user, puzzle, play=True)):
        raise HTTPException(404, "Puzzle not found")
    try:
        require_puzzle_access(request, puzzle, password)
    except HTTPException as error:
        if error.status_code == 429:
            raise
        return templates.TemplateResponse(request, "puzzle_unlock.html", {
            "request": request,
            "puzzle": puzzle,
            "current_user": user,
            "return_to": return_to,
            "error": "That puzzle password was not accepted.",
        }, status_code=423)
    target = return_to if return_to.startswith(f"/puzzles/{puzzle.id}") else f"/puzzles/{puzzle.id}"
    return RedirectResponse(target, status_code=303)


@app.get("/puzzles/{puzzle_id}/print", response_class=HTMLResponse)
def puzzle_print_page(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_web_series_puzzle(request, db, user, puzzle_id)
    return render_puzzle_print_page(puzzle_id, request, db, user, show_toolbar=True)


@app.get("/puzzles/{puzzle_id}/print/render", response_class=HTMLResponse)
def puzzle_print_render_page(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    if token := request.query_params.get('render_token'):
        try:
            payload = URLSafeTimedSerializer(SESSION_SECRET,salt='puzzary-print-render').loads(token,max_age=900)
        except BadSignature:
            raise HTTPException(401,'Print link expired or invalid; request a new print preview')
        user = db.get(User,payload.get('user_id'))
        if payload.get('puzzle_id') != puzzle_id or not user or payload.get('session_version') != user.session_version:
            raise HTTPException(401,'Print link expired or invalid')
        puzzle = db.get(Puzzle, puzzle_id)
        if puzzle and puzzle.access_password_hash and not hmac.compare_digest(
            str(payload.get('puzzle_access', '')), puzzle_unlock_fingerprint(puzzle)
        ):
            raise HTTPException(401, 'Print link expired or invalid')
        if payload.get('api'): require_permission(user,'api.use')
        authorize_request(request,db,user)
    else:
        user = require_api_user(request,db)
    response = render_puzzle_print_page(puzzle_id, request, db, user, show_toolbar=False)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.get("/puzzles/{puzzle_id}/print.pdf")
def puzzle_pdf_download(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_web_series_puzzle(request, db, user, puzzle_id)
    return puzzle_pdf_response(puzzle_id, request, db, user)


@app.get("/api/puzzles/{puzzle_id}/print.pdf")
def api_puzzle_pdf(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_api_user(request, db)
    return puzzle_pdf_response(puzzle_id, request, db, user)


@app.get("/api/puzzles/{puzzle_id}/print.png")
def api_puzzle_png(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    """Return a printable puzzle as an image for automation consumers."""
    user = require_api_user(request, db)
    rendered = render_puzzle_print_page(puzzle_id, request, db, user, show_toolbar=False, pdf_mode=True)
    layout = rendered.context["print_layout"]
    try:
        image = render_png(rendered.body.decode("utf-8"), layout)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "10"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(status_code=503, detail="Image rendering failed. Please retry.") from exc
    return Response(image, media_type="image/png", headers={
        "Content-Disposition": f'inline; filename="puzzary-{puzzle_id}.png"',
        "Cache-Control": "private, no-store",
    })


def puzzle_pdf_response(puzzle_id: str, request: Request, db: Session, user: User) -> Response:
    rendered = render_puzzle_print_page(puzzle_id, request, db, user, show_toolbar=False, pdf_mode=True)
    layout = rendered.context["print_layout"]
    try:
        pdf = render_pdf(rendered.body.decode("utf-8"), layout)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "10"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(status_code=503, detail="PDF rendering failed. Please retry.") from exc
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="puzzary-{puzzle_id}-{layout["page_size"]}.pdf"',
        "Cache-Control": "private, no-store",
    })


def render_puzzle_print_page(
    puzzle_id: str,
    request: Request,
    db: Session,
    user: User | None,
    show_toolbar: bool,
    pdf_mode: bool = False,
) -> HTMLResponse:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    if user is not None:
        require_series_access(db, puzzle, user, series_group_for_request(request, db, puzzle))
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if puzzle_type is None or not puzzle_type.supports_print or puzzle_type.digital_only:
        raise HTTPException(status_code=404, detail="This puzzle does not support printing")
    print_layout = apply_print_request_layout(print_layout_context(puzzle, puzzle_type, db), request)
    return templates.TemplateResponse(
        request,
        "print.html",
        {
            "request": request,
            "puzzle": puzzle,
            "current_user": user,
            "puzzle_type_definition": puzzle_type,
            "print_payload": puzzle.print_payload or {},
            "has_module_print_template": template_exists(f"puzzle_print/{puzzle.puzzle_type}.html"),
            "print_layout": print_layout,
            "print_gimme_cells": printable_gimme_cells(puzzle),
            "print_gimme_words": printable_gimme_words(puzzle),
            "show_print_toolbar": show_toolbar,
            "hide_app_chrome": not show_toolbar,
            "print_pdf_css": (BASE_DIR / "static" / "styles.css").read_text(encoding="utf-8") if pdf_mode else None,
        },
    )


@app.post("/web/puzzles/{puzzle_id}/attempts")
def web_attempt_puzzle(
    puzzle_id: str,
    request: Request,
    answer: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_web_series_puzzle(request, db, user, puzzle_id)
    result = submit_puzzle_attempt(puzzle_id, answer, user.id, db)
    celebration = "?celebrate=1" if result.is_correct else ""
    return RedirectResponse(url=f"/puzzles/{puzzle_id}{celebration}", status_code=303)


@app.post("/web/puzzles/{puzzle_id}/photos")
def web_upload_photo(
    puzzle_id: str,
    request: Request,
    notes: str = Form(""),
    attempt_id: int | None = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_web_series_puzzle(request, db, user, puzzle_id)
    save_photo(puzzle_id, file, notes, user.id, db, attempt_id=attempt_id)
    return RedirectResponse(url=f"/puzzles/{puzzle_id}", status_code=303)


@app.get("/web/puzzles/{puzzle_id}/photos/{photo_id}/content")
def web_photo_content(
    puzzle_id: str,
    photo_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    photo = db.get(PuzzlePhoto, photo_id)
    if photo is None or photo.puzzle_id != puzzle_id:
        raise HTTPException(status_code=404, detail="Photo submission not found")
    if photo.user_id != user.id and not user.can("activity.view"):
        raise HTTPException(status_code=403, detail="Photo submission access denied")
    target = stored_photo_path(photo.file_path)
    return FileResponse(
        target,
        media_type=photo_media_type(target),
        filename=photo.original_filename,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/web/puzzles/{puzzle_id}/photos/{photo_id}/delete")
def web_delete_photo(
    puzzle_id: str,
    photo_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    photo = db.get(PuzzlePhoto, photo_id)
    if photo is None or photo.puzzle_id != puzzle_id:
        raise HTTPException(status_code=404, detail="Photo submission not found")
    if photo.user_id != user.id and not user.can("activity.view"):
        raise HTTPException(status_code=403, detail="Photo submission access denied")
    file_path = photo.file_path
    db.delete(photo)
    db.commit()
    delete_photo_file(file_path)
    remove_empty_photo_directory(puzzle_id)
    return RedirectResponse(url=f"/puzzles/{puzzle_id}", status_code=303)


@app.post("/web/puzzles/{puzzle_id}/reset")
def web_reset_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    require_web_series_puzzle(request, db, user, puzzle_id)
    reset_user_puzzle(puzzle_id, user.id, db)
    return RedirectResponse(url=f"/puzzles/{puzzle_id}", status_code=303)


@app.post("/api/users")
def create_user(payload: UserCreate, request: Request, db: Session = Depends(get_db)) -> dict[str, int | str | bool]:
    actor = require_admin_api(request, db)
    user, setup_token = create_user_record(db, payload.display_name)
    audit(db, actor, 'user', user.id, 'user.created', {}, {'display_name':user.display_name,'roles':[r.id for r in user.roles]})
    db.commit()
    return {
        "id": user.id,
        "display_name": user.display_name,
        "requires_password_setup": bool(setup_token),
        "setup_url": setup_url(setup_token, db) if setup_token else "",
    }


@app.get("/api/admin/oidc/providers")
def api_list_oidc_providers(request: Request, db: Session = Depends(get_db)) -> dict:
    user = require_admin_api(request, db)
    require_permission(user, "settings.manage")
    return {
        "login_mode": oidc_login_mode(db),
        "local_accounts_default": True,
        "local_admin_recovery": True,
        "callback_url": oidc_callback_url(db),
        "providers": [
            oidc_provider_payload(provider, db)
            for provider in db.scalars(select(OIDCProvider).order_by(OIDCProvider.display_name, OIDCProvider.id))
        ],
    }


@app.post("/api/admin/oidc/providers", status_code=201)
def api_create_oidc_provider(
    payload: OIDCProviderWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = require_admin_api(request, db)
    require_permission(user, "settings.manage")
    provider = OIDCProvider(created_by_id=user.id)
    try:
        update_oidc_provider(
            provider,
            display_name=payload.display_name,
            issuer_url=payload.issuer_url,
            client_id=payload.client_id,
            client_secret=payload.client_secret,
            scopes=payload.scopes,
            provisioning_mode=payload.provisioning_mode,
            enabled=payload.enabled,
        )
        if db.scalar(select(OIDCProvider.id).where(OIDCProvider.issuer_url == provider.issuer_url)) is not None:
            raise OIDCError("A provider with that issuer already exists.")
    except OIDCError as oidc_error:
        raise HTTPException(400, str(oidc_error)) from oidc_error
    db.add(provider)
    db.flush()
    audit(db, user, "oidc_provider", provider.id, "oidc.created", {}, {"issuer": provider.issuer_url})
    db.commit()
    db.refresh(provider)
    return oidc_provider_payload(provider, db)


@app.patch("/api/admin/oidc/providers/{provider_id}")
def api_update_oidc_provider(
    provider_id: int,
    payload: OIDCProviderPatch,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = require_admin_api(request, db)
    require_permission(user, "settings.manage")
    provider = db.get(OIDCProvider, provider_id)
    if provider is None:
        raise HTTPException(404, "OIDC provider not found")
    values = payload.model_dump(exclude_unset=True)
    before = oidc_provider_payload(provider, db)
    try:
        update_oidc_provider(
            provider,
            display_name=values.get("display_name", provider.display_name),
            issuer_url=values.get("issuer_url", provider.issuer_url),
            client_id=values.get("client_id", provider.client_id),
            client_secret=values.get("client_secret") or None,
            scopes=values.get("scopes", provider.scopes),
            provisioning_mode=values.get("provisioning_mode", provider.provisioning_mode),
            enabled=values.get("enabled", provider.enabled),
        )
        duplicate = db.scalar(
            select(OIDCProvider.id).where(
                OIDCProvider.issuer_url == provider.issuer_url,
                OIDCProvider.id != provider.id,
            )
        )
        if duplicate is not None:
            raise OIDCError("A provider with that issuer already exists.")
    except OIDCError as oidc_error:
        db.rollback()
        raise HTTPException(400, str(oidc_error)) from oidc_error
    restore_local_login_if_no_oidc_provider(db)
    audit(db, user, "oidc_provider", provider.id, "oidc.updated", before, oidc_provider_payload(provider, db))
    db.commit()
    return oidc_provider_payload(provider, db)


@app.delete("/api/admin/oidc/providers/{provider_id}", status_code=204)
def api_delete_oidc_provider(provider_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    user = require_admin_api(request, db)
    require_permission(user, "settings.manage")
    provider = db.get(OIDCProvider, provider_id)
    if provider is None:
        raise HTTPException(404, "OIDC provider not found")
    identity_count = db.scalar(
        select(func.count()).select_from(OIDCIdentity).where(OIDCIdentity.provider_id == provider.id)
    ) or 0
    if provider.enabled or identity_count:
        raise HTTPException(409, "Disable the provider and remove linked identities before deleting it")
    audit(db, user, "oidc_provider", provider.id, "oidc.deleted", oidc_provider_payload(provider, db), {})
    db.delete(provider)
    restore_local_login_if_no_oidc_provider(db)
    db.commit()
    return Response(status_code=204)


@app.put("/api/admin/oidc/settings")
def api_update_oidc_settings(
    payload: OIDCLoginSettingsWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = require_admin_api(request, db)
    require_permission(user, "settings.manage")
    mode = payload.login_mode.strip().lower()
    if mode not in OIDC_LOGIN_MODES:
        raise HTTPException(400, "Unknown login mode")
    if mode != "local" and not oidc_enabled_providers(db):
        raise HTTPException(409, "Enable a successfully tested OIDC provider first")
    set_app_setting(db, "oidc_login_mode", mode)
    audit(db, user, "settings", "oidc_login_mode", "oidc.login_mode", {}, {"mode": mode})
    db.commit()
    return {"login_mode": mode, "local_admin_recovery": True}


@app.post("/api/admin/oidc/providers/{provider_id}/test")
def api_test_oidc_provider(provider_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    user = require_admin_api(request, db)
    require_permission(user, "settings.manage")
    provider = db.get(OIDCProvider, provider_id)
    if provider is None:
        raise HTTPException(404, "OIDC provider not found")
    try:
        authorization_url = begin_oidc_authorization(
            db, provider, intent="test", user_id=user.id, return_to="/admin/settings"
        )
    except OIDCError as oidc_error:
        raise HTTPException(400, str(oidc_error)) from oidc_error
    return {"authorization_url": authorization_url, "callback_url": oidc_callback_url(db)}


def apply_definition_payload(
    db: Session,
    definition: DictionaryDefinition,
    payload: DictionaryDefinitionWrite | DictionaryDefinitionPatch,
) -> None:
    if payload.definition is not None:
        definition.definition = payload.definition.strip()
    if payload.part_of_speech is not None:
        definition.part_of_speech = payload.part_of_speech.strip()
    if payload.example is not None:
        definition.example = payload.example.strip()
    if payload.position is not None:
        definition.position = payload.position
    if payload.properties is not None:
        definition.properties = payload.properties
    if isinstance(payload, DictionaryDefinitionPatch) and payload.archived is not None:
        definition.archived_at = datetime.now(timezone.utc) if payload.archived else None
    if payload.tag_ids is not None or payload.clues is not None or payload.part_of_speech is not None:
        tag_ids = payload.tag_ids if payload.tag_ids is not None else [tag.id for tag in definition.tags if tag.tag_type != "Clue"]
        clues = payload.clues if payload.clues is not None else stored_definition_clues(definition)
        part_of_speech = payload.part_of_speech if payload.part_of_speech is not None else definition.part_of_speech
        definition.properties = properties_with_clues(definition.properties, clues)
        definition.tags = definition_tags_from_form(db, tag_ids, part_of_speech, "\n".join(clues))


def apply_word_bank_payload(db: Session, bank: WordBank, payload: WordBankWrite | WordBankPatch) -> None:
    existing_payload = bank.payload if isinstance(bank.payload, dict) else {}
    if payload.name is not None:
        bank.name = payload.name.strip()
    if payload.slug is not None:
        bank.slug = unique_slug(db, WordBank, payload.slug or bank.name, bank.id)
    if payload.description is not None:
        bank.description = payload.description.strip()
    if payload.mode is not None:
        existing_payload["mode"] = payload.mode if payload.mode == "automatic" else "manual"
    if payload.properties is not None:
        existing_payload["properties"] = payload.properties
    if payload.words is not None:
        parsed_words = parse_words("\n".join(payload.words))
        existing_payload["words"] = parsed_words
        if existing_payload.get("mode") != "automatic":
            sync_word_bank_words(db, bank, parsed_words)
    if payload.category_id is not None:
        bank.category_id = payload.category_id or None
    if payload.tag_ids is not None:
        bank.tags = tags_for_ids(db, payload.tag_ids)
    if isinstance(payload, WordBankPatch) and payload.archived is not None:
        bank.archived_at = datetime.now(timezone.utc) if payload.archived else None
    bank.payload = existing_payload


@app.get("/api/dictionary")
def api_list_dictionary(
    request: Request,
    search: str = "",
    include_archived: bool = False,
    include_banned: bool = True,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    query = select(DictionaryWord).order_by(DictionaryWord.word)
    words = db.scalars(query).all()
    if search:
        search_text = search.strip().lower()
        words = [word for word in words if search_text in word.word.lower() or search_text in word.display_text.lower()]
    if not include_archived:
        words = [word for word in words if word.archived_at is None]
    if not include_banned:
        words = [word for word in words if word.banned_at is None]
    total = len(words)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [serialize_dictionary_word(word) for word in words[offset : offset + limit]],
    }


@app.post("/api/dictionary/import")
def api_import_dictionary(payload: DictionaryImportRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    try:
        sources = fetch_dictionary_source(payload.source_url, payload.source_type, payload.source_api_token)
        imported_words = parse_imported_words(sources)
        result = import_dictionary_words(
            db,
            imported_words,
            source_label=payload.source_label or payload.source_url,
            tag_ids=payload.tag_ids,
            dry_run=payload.dry_run,
            max_items=payload.max_items,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    result["source_count"] = len(sources)
    result["source_url"] = payload.source_url
    result["source_type"] = payload.source_type
    return result


@app.get("/api/dictionary/export")
def api_export_dictionary(
    request: Request,
    include_archived: bool = False,
    include_banned: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    words = db.scalars(select(DictionaryWord).order_by(DictionaryWord.word)).all()
    if not include_archived:
        words = [word for word in words if word.archived_at is None]
    if not include_banned:
        words = [word for word in words if word.banned_at is None]
    return dictionary_export_payload(words)


@app.get("/api/dictionary/import-sources")
def api_list_dictionary_import_sources(request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    sources = db.scalars(select(DictionaryImportSource).order_by(DictionaryImportSource.name)).all()
    return {"items": [dictionary_import_source_summary(source) for source in sources]}


@app.post("/api/dictionary/import-sources")
def api_create_dictionary_import_source(
    payload: DictionaryImportSourceWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    source = DictionaryImportSource(
        name=payload.name.strip(),
        source_url=payload.source_url.strip(),
        source_type=payload.source_type.strip() or "auto",
        source_label=payload.source_label.strip(),
        source_api_token=payload.source_api_token.strip(),
        sync_interval_hours=max(1, min(payload.sync_interval_hours, 24 * 30)),
        max_items=max(1, min(payload.max_items, 5000)),
        enabled=payload.enabled,
    )
    schedule_next_dictionary_import(source, utcnow())
    db.add(source)
    db.commit()
    db.refresh(source)
    if payload.sync_now:
        run_dictionary_import_source(db, source, dry_run=False)
        db.refresh(source)
    return dictionary_import_source_summary(source)


@app.post("/api/dictionary/import-sources/{source_id}/sync")
def api_sync_dictionary_import_source(source_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    source = db.get(DictionaryImportSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Dictionary import source not found")
    result = run_dictionary_import_source(db, source, dry_run=False)
    return {"source": dictionary_import_source_summary(source), "result": result}


@app.delete("/api/dictionary/import-sources/{source_id}")
def api_delete_dictionary_import_source(source_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    source = db.get(DictionaryImportSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Dictionary import source not found")
    db.delete(source)
    db.commit()
    return {"deleted": True, "id": source_id}


@app.post("/api/dictionary")
def api_create_dictionary_word(payload: DictionaryWordWrite, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    word_key = normalize_dictionary_word(payload.word)
    if not word_key:
        raise HTTPException(status_code=400, detail="Word must contain at least one letter or number")
    existing = db.scalar(select(DictionaryWord).where(DictionaryWord.word == word_key))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Dictionary word already exists")
    word = DictionaryWord(
        word=word_key,
        display_text=payload.display_text.strip() or payload.word.strip(),
        clue=payload.clue.strip(),
        part_of_speech=payload.part_of_speech.strip(),
        properties=payload.properties,
    )
    word.tags = tags_for_ids(db, payload.tag_ids)
    db.add(word)
    db.flush()
    for index, definition_payload in enumerate(payload.definitions, start=1):
        definition = DictionaryDefinition(
            word=word,
            definition=definition_payload.definition.strip(),
            part_of_speech=definition_payload.part_of_speech.strip(),
            example=definition_payload.example.strip(),
            source="api",
            position=definition_payload.position or index,
            properties=definition_payload.properties,
        )
        db.add(definition)
        db.flush()
        apply_definition_payload(db, definition, definition_payload)
    db.commit()
    db.refresh(word)
    return serialize_dictionary_word(word)


@app.get("/api/dictionary/{word_id}")
def api_get_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    word = db.get(DictionaryWord, word_id)
    if word is None:
        raise HTTPException(status_code=404, detail="Dictionary word not found")
    return serialize_dictionary_word(word)


@app.patch("/api/dictionary/{word_id}")
def api_update_dictionary_word(
    word_id: int,
    payload: DictionaryWordPatch,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    word = db.get(DictionaryWord, word_id)
    if word is None:
        raise HTTPException(status_code=404, detail="Dictionary word not found")
    if payload.display_text is not None:
        word.display_text = payload.display_text.strip() or word.display_text
    if payload.clue is not None:
        word.clue = payload.clue.strip()
    if payload.part_of_speech is not None:
        word.part_of_speech = payload.part_of_speech.strip()
    if payload.properties is not None:
        word.properties = payload.properties
    if payload.tag_ids is not None:
        word.tags = tags_for_ids(db, payload.tag_ids)
    if payload.archived is not None:
        word.archived_at = datetime.now(timezone.utc) if payload.archived else None
    if payload.banned is not None:
        word.banned_at = datetime.now(timezone.utc) if payload.banned else None
    db.commit()
    db.refresh(word)
    return serialize_dictionary_word(word)


@app.delete("/api/dictionary/{word_id}")
def api_delete_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    word = db.get(DictionaryWord, word_id)
    if word is None:
        raise HTTPException(status_code=404, detail="Dictionary word not found")
    for definition in word.definitions:
        definition.tags = []
    word.tags = []
    db.delete(word)
    db.commit()
    return {"deleted": True, "id": word_id}


@app.post("/api/dictionary/{word_id}/definitions")
def api_create_dictionary_definition(
    word_id: int,
    payload: DictionaryDefinitionWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    word = db.get(DictionaryWord, word_id)
    if word is None:
        raise HTTPException(status_code=404, detail="Dictionary word not found")
    definition = DictionaryDefinition(
        word=word,
        definition=payload.definition.strip(),
        part_of_speech=payload.part_of_speech.strip(),
        example=payload.example.strip(),
        source="api",
        position=payload.position,
        properties=payload.properties,
    )
    db.add(definition)
    db.flush()
    apply_definition_payload(db, definition, payload)
    db.commit()
    db.refresh(definition)
    return serialize_definition(definition)


@app.patch("/api/dictionary/{word_id}/definitions/{definition_id}")
def api_update_dictionary_definition(
    word_id: int,
    definition_id: int,
    payload: DictionaryDefinitionPatch,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    definition = db.get(DictionaryDefinition, definition_id)
    if definition is None or definition.word_id != word_id:
        raise HTTPException(status_code=404, detail="Dictionary definition not found")
    apply_definition_payload(db, definition, payload)
    db.commit()
    db.refresh(definition)
    return serialize_definition(definition)


@app.delete("/api/dictionary/{word_id}/definitions/{definition_id}")
def api_delete_dictionary_definition(
    word_id: int,
    definition_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    definition = db.get(DictionaryDefinition, definition_id)
    if definition is None or definition.word_id != word_id:
        raise HTTPException(status_code=404, detail="Dictionary definition not found")
    definition.tags = []
    db.delete(definition)
    db.commit()
    return {"deleted": True, "id": definition_id}


@app.get("/api/tags")
def api_list_tags(
    request: Request,
    search: str = "",
    tag_type: str = "",
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    tags = db.scalars(select(PuzzleTag).order_by(PuzzleTag.tag_type, PuzzleTag.name)).all()
    if tag_type.strip():
        normalized_type = normalize_tag_type(tag_type)
        tags = [tag for tag in tags if tag.tag_type == normalized_type]
    if search.strip():
        search_text = search.strip().lower()
        tags = [
            tag for tag in tags
            if search_text in tag.name.lower() or search_text in tag.slug.lower() or search_text in tag.label.lower()
        ]
    total = len(tags)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [serialize_tag(tag) for tag in tags[offset : offset + limit]],
    }


@app.post("/api/tags")
def api_create_tag(payload: PuzzleTagWrite, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    normalized_type = normalize_tag_type(payload.tag_type)
    tag = PuzzleTag(
        name=payload.name.strip(),
        tag_type=normalized_type,
        slug=unique_slug(db, PuzzleTag, payload.slug or typed_tag_slug(normalized_type, payload.name)),
    )
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return serialize_tag(tag)


@app.get("/api/tags/{tag_id}")
def api_get_tag(tag_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    tag = db.get(PuzzleTag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    return serialize_tag(tag)


@app.patch("/api/tags/{tag_id}")
def api_update_tag(tag_id: int, payload: PuzzleTagPatch, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    tag = db.get(PuzzleTag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    new_name = payload.name.strip() if payload.name is not None else tag.name
    new_type = normalize_tag_type(payload.tag_type) if payload.tag_type is not None else tag.tag_type
    tag.name = new_name
    tag.tag_type = new_type
    if payload.slug is not None or payload.name is not None or payload.tag_type is not None:
        tag.slug = unique_slug(db, PuzzleTag, payload.slug or typed_tag_slug(new_type, new_name), tag.id)
    db.commit()
    db.refresh(tag)
    return serialize_tag(tag)


@app.delete("/api/tags/{tag_id}")
def api_delete_tag(tag_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    tag = db.get(PuzzleTag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    tag.puzzles = []
    tag.word_banks = []
    tag.dictionary_words = []
    tag.dictionary_definitions = []
    db.delete(tag)
    db.commit()
    return {"deleted": True, "id": tag_id}


@app.get("/api/word-banks")
def api_list_word_banks(
    request: Request,
    search: str = "",
    include_archived: bool = False,
    include_words: bool = False,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    query = select(WordBank).order_by(WordBank.name)
    if not include_archived:
        query = query.where(WordBank.archived_at.is_(None))
    banks = db.scalars(query).all()
    if search.strip():
        search_text = search.strip().lower()
        banks = [
            bank for bank in banks
            if search_text in bank.name.lower() or search_text in bank.slug.lower() or search_text in bank.description.lower()
        ]
    total = len(banks)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [serialize_word_bank(bank, include_words=include_words) for bank in banks[offset : offset + limit]],
    }


@app.get("/api/word-banks/{bank_id}/entry-options")
def api_word_bank_entry_options(
    bank_id: int,
    request: Request,
    q: str = "",
    limit: int = 40,
    db: Session = Depends(get_db),
) -> dict:
    user = require_api_user(request, db)
    require_permission(user, "content.manage")
    bank = db.get(WordBank, bank_id)
    if bank is None:
        raise HTTPException(status_code=404, detail="Word bank not found")
    if word_bank_mode(bank) != "manual" or bank.archived_at is not None:
        raise HTTPException(status_code=409, detail="Choose an active manual word bank")
    query_text = q.strip()[:80]
    if len(query_text) < 2:
        return {"items": [], "query": query_text, "has_more": False}
    escaped = query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    existing_word_ids = select(WordBankWord.word_id).where(WordBankWord.word_bank_id == bank.id)
    matches = (
        select(DictionaryWord.id)
        .outerjoin(DictionaryDefinition, DictionaryDefinition.word_id == DictionaryWord.id)
        .where(
            DictionaryWord.archived_at.is_(None),
            DictionaryWord.banned_at.is_(None),
            DictionaryWord.id.not_in(existing_word_ids),
            or_(
                DictionaryWord.word.ilike(pattern, escape="\\"),
                DictionaryWord.display_text.ilike(pattern, escape="\\"),
                DictionaryWord.clue.ilike(pattern, escape="\\"),
                DictionaryDefinition.definition.ilike(pattern, escape="\\"),
            ),
        )
        .distinct()
        .order_by(DictionaryWord.id)
        .limit(max(1, min(limit, 60)) + 1)
    )
    word_ids = list(db.scalars(matches))
    has_more = len(word_ids) > max(1, min(limit, 60))
    word_ids = word_ids[:max(1, min(limit, 60))]
    if not word_ids:
        return {"items": [], "query": query_text, "has_more": False}
    words = db.scalars(
        select(DictionaryWord)
        .where(DictionaryWord.id.in_(word_ids))
        .options(selectinload(DictionaryWord.definitions).selectinload(DictionaryDefinition.tags))
        .order_by(func.lower(DictionaryWord.display_text), DictionaryWord.id)
    ).all()
    items = []
    for word in words:
        display = word.display_text or word.word
        items.append({
            "value": f"{word.id}:0", "word": display, "definition": "Any active definition",
            "label": f"{display} / any definition",
        })
        for definition in word.definitions:
            if definition.archived_at is not None:
                continue
            clue = definition_clue(definition).strip()
            description = clue[:240] or "Definition without display text"
            prefix = f"{definition.part_of_speech}: " if definition.part_of_speech else ""
            items.append({
                "value": f"{word.id}:{definition.id}", "word": display,
                "definition": prefix + description,
                "label": f"{display} / {prefix}{description}",
            })
    return {"items": items, "query": query_text, "has_more": has_more}


@app.post("/api/word-banks")
def api_create_word_bank(payload: WordBankWrite, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    mode = payload.mode if payload.mode == "automatic" else "manual"
    bank = WordBank(
        name=payload.name.strip(),
        slug=unique_slug(db, WordBank, payload.slug or payload.name),
        description=payload.description.strip(),
        category_id=payload.category_id or None,
        payload={"mode": mode, "words": [], "properties": payload.properties},
    )
    bank.tags = tags_for_ids(db, payload.tag_ids)
    db.add(bank)
    db.flush()
    if payload.words:
        apply_word_bank_payload(db, bank, payload)
    db.commit()
    db.refresh(bank)
    return serialize_word_bank(bank, include_words=True)


@app.get("/api/word-banks/{bank_id}")
def api_get_word_bank(
    bank_id: int,
    request: Request,
    include_words: bool = True,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    bank = db.get(WordBank, bank_id)
    if bank is None:
        raise HTTPException(status_code=404, detail="Word bank not found")
    return serialize_word_bank(bank, include_words=include_words)


@app.patch("/api/word-banks/{bank_id}")
def api_update_word_bank(
    bank_id: int,
    payload: WordBankPatch,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    bank = db.get(WordBank, bank_id)
    if bank is None:
        raise HTTPException(status_code=404, detail="Word bank not found")
    apply_word_bank_payload(db, bank, payload)
    db.commit()
    db.refresh(bank)
    return serialize_word_bank(bank, include_words=True)


@app.delete("/api/word-banks/{bank_id}")
def api_delete_word_bank(bank_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    bank = db.get(WordBank, bank_id)
    if bank is None:
        raise HTTPException(status_code=404, detail="Word bank not found")
    for puzzle in bank.puzzles:
        puzzle.word_bank_id = None
    bank.tags = []
    db.delete(bank)
    db.commit()
    return {"deleted": True, "id": bank_id}


@app.post("/api/word-banks/{bank_id}/words")
def api_add_word_bank_word(
    bank_id: int,
    payload: WordBankEntryWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    bank = db.get(WordBank, bank_id)
    word = db.get(DictionaryWord, payload.word_id)
    definition = db.get(DictionaryDefinition, payload.definition_id) if payload.definition_id else None
    if bank is None:
        raise HTTPException(status_code=404, detail="Word bank not found")
    if word is None:
        raise HTTPException(status_code=404, detail="Dictionary word not found")
    if definition is not None and definition.word_id != word.id:
        raise HTTPException(status_code=400, detail="Definition does not belong to the supplied word")
    existing = db.scalar(
        select(WordBankWord).where(WordBankWord.word_bank_id == bank_id, WordBankWord.word_id == word.id)
    )
    if existing is None:
        next_position = payload.position if payload.position is not None else len(bank.word_links) + 1
        existing = WordBankWord(word_bank=bank, word=word, definition=definition, position=next_position)
        db.add(existing)
    else:
        existing.definition = definition
        if payload.position is not None:
            existing.position = payload.position
    bank_payload = bank.payload if isinstance(bank.payload, dict) else {}
    bank_payload["mode"] = "manual"
    bank.payload = bank_payload
    db.commit()
    db.refresh(existing)
    return serialize_word_bank_link(existing)


@app.delete("/api/word-banks/{bank_id}/words/{link_id}")
def api_delete_word_bank_word(
    bank_id: int,
    link_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    link = db.get(WordBankWord, link_id)
    if link is None or link.word_bank_id != bank_id:
        raise HTTPException(status_code=404, detail="Word bank entry not found")
    db.delete(link)
    db.commit()
    return {"deleted": True, "id": link_id}


def create_puzzle_record(
    payload: PuzzleGenerateRequest,
    db: Session,
    creator: User | None = None,
    *,
    notify: bool = True,
) -> PuzzleSummary:
    puzzle_type = get_puzzle_type(payload.puzzle_type)
    if puzzle_type is None:
        raise HTTPException(status_code=400, detail="Unsupported puzzle type")
    if not is_puzzle_type_enabled(db, payload.puzzle_type):
        raise HTTPException(status_code=400, detail="This puzzle type is disabled")
    generation_options = validate_generation_options(puzzle_type, payload.generation_options)

    needs_word_bank = "word_bank" in puzzle_type.source_requirements
    selected_banks = []
    if needs_word_bank:
        selected_banks = word_banks_for_slugs(db, payload.word_bank_slugs)
        if not selected_banks and payload.theme:
            selected_banks = word_banks_for_slugs(db, [payload.theme])
    word_bank = selected_banks[0] if len(selected_banks) == 1 else None
    if needs_word_bank and not selected_banks:
        raise HTTPException(status_code=400, detail="This puzzle type requires a word bank source")
    generation_theme = word_bank_source_theme(selected_banks, payload.theme) if selected_banks else payload.theme
    excluded_tag_ids = set(payload.excluded_tag_ids)
    excluded_tags = tags_for_ids(db, payload.excluded_tag_ids)
    source_entries = combined_word_bank_entries(db, selected_banks, excluded_tag_ids) if selected_banks else []
    source_data = puzzle_type.source_builder(source_entries, payload.difficulty, excluded_tag_ids) if selected_banks else None
    if selected_banks and source_data is None:
        raise HTTPException(status_code=400, detail="No usable words remain after archive, ban, and tag exclusions")
    if payload.puzzle_type == "word_search":
        source_data = {
            "words": source_data,
            "blocked_words": blocked_words_for_generation(db, excluded_tag_ids),
        }
    generation_seed = normalize_seed(payload.seed)
    try:
        generated = puzzle_type.generator(generation_theme, payload.difficulty, source_data, generation_seed, generation_options)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    generated.metadata["excluded_tag_ids"] = sorted(excluded_tag_ids)
    generated.metadata["excluded_tag_slugs"] = [tag.slug for tag in excluded_tags]
    generated.metadata["seed"] = generation_seed
    if selected_banks:
        generated.metadata["word_bank_slugs"] = [bank.slug for bank in selected_banks]
        generated.metadata["word_bank_names"] = [bank.name for bank in selected_banks]
    draft = bool(creator and not can_owned(creator,'puzzles','publish',creator.id))
    schedule_at = require_future_publish_at(payload.publish_at) if payload.publish_at else None
    if schedule_at is not None and creator is not None:
        require_owned(creator, 'puzzles', 'publish', creator.id)
        require_owned(creator, 'puzzles', 'schedule', creator.id)
    generated.metadata['draft'] = draft
    generated.metadata['visibility'] = payload.visibility
    generated.metadata['offline_disabled'] = not payload.offline_allowed
    puzzle = Puzzle(
        id=uuid.uuid4().hex[:12],
        puzzle_type=payload.puzzle_type,
        title=generated.title,
        theme=generation_theme,
        difficulty=payload.difficulty,
        word_bank_id=word_bank.id if word_bank is not None else None,
        prompt=generated.prompt,
        completion_message=normalize_completion_message(payload.completion_message),
        print_payload=generated.print_payload,
        play_payload=generated.play_payload,
        solution_payload=generated.solution_payload,
        puzzle_metadata=generated.metadata,
        publish_at=None if draft else schedule_at,
        auto_archive_at=None if draft else default_auto_archive_at(schedule_at),
        archived_at=utcnow() if draft else None,
        created_by_id=creator.id if creator is not None else None,
    )
    try:
        update_puzzle_access_password(puzzle, payload.access_password)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    db.add(puzzle)
    db.flush()
    if creator is not None and not draft:
        audit(db, creator, "puzzle", puzzle.id, "puzzle.scheduled" if schedule_at else "puzzle.published", {}, {
            "state": "scheduled" if schedule_at else "published",
            "publish_at": schedule_at.isoformat() if schedule_at else None,
        })
    for index, hint_text in enumerate(generated.hints, start=1):
        db.add(PuzzleHint(puzzle_id=puzzle.id, order=index, text=hint_text))
    if notify and not draft and schedule_at is None:
        puzzle_notifications.queue_puzzle_publication(db, puzzle)
    db.commit()
    db.refresh(puzzle)
    return serialize_puzzle(puzzle, db)


@app.post("/api/puzzles/generate", response_model=PuzzleSummary)
def create_puzzle(payload: PuzzleGenerateRequest, request: Request, db: Session = Depends(get_db)) -> PuzzleSummary:
    user = require_admin_api(request, db)
    return create_puzzle_record(payload, db, creator=user)


@app.get("/api/puzzle-types")
def list_puzzle_type_contracts(
    request: Request,
    include_disabled: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    return {"puzzle_types": list(puzzle_type_generation_schemas(db, include_disabled=include_disabled).values())}


@app.get("/api/plugin-packages")
def list_plugin_packages(request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    return plugin_management_context(db)


@app.get("/api/plugin-packages/catalog")
def get_plugin_catalog(request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    return official_plugin_catalog()


@app.get("/api/plugin-packages/github-access")
def get_plugin_github_access(request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    return plugin_github_access(db)


@app.put("/api/plugin-packages/github-access")
def put_plugin_github_access(
    payload: PluginGitHubAccessWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = require_admin_api(request, db)
    try:
        save_plugin_github_token(db, user, payload.token)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return plugin_github_access(db)


@app.post("/api/plugin-packages/github-access/test")
def api_test_plugin_github_access(request: Request, db: Session = Depends(get_db)) -> dict:
    user = require_admin_api(request, db)
    try:
        result = check_plugin_github_token(db, user)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"accessible": True, **result, "source": plugin_github_access(db)["source"]}


@app.delete("/api/plugin-packages/github-access")
def delete_plugin_github_access(request: Request, db: Session = Depends(get_db)) -> dict:
    user = require_admin_api(request, db)
    remove_plugin_github_token(db, user)
    return plugin_github_access(db)


@app.post("/api/plugin-packages/install")
def install_plugin_package(
    payload: PluginInstallRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = require_admin_api(request, db)
    try:
        manifest = install_plugin_for_user(db, user, payload.url, payload.sha256)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "slug": manifest["slug"],
        "name": manifest.get("name", manifest["slug"]),
        "version": manifest.get("version", ""),
        "restart_required": True,
    }


@app.post("/api/plugin-packages/install-official")
def install_official_plugin_packages(
    payload: OfficialPluginInstallRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = require_admin_api(request, db)
    catalog = official_plugin_catalog()["plugins"]
    requested = set(payload.slugs)
    known = {plugin["slug"] for plugin in catalog}
    if requested - known:
        raise HTTPException(status_code=422, detail=f"Unknown official plugins: {sorted(requested - known)}")
    selected = catalog if not requested else [plugin for plugin in catalog if plugin["slug"] in requested]
    installed = []
    try:
        for plugin in selected:
            manifest = install_plugin_for_user(db, user, plugin["url"], plugin["sha256"])
            installed.append({"slug": manifest["slug"], "version": manifest.get("version", "")})
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc), "installed": installed}) from exc
    return {"installed": installed, "restart_required": True}


@app.post("/api/plugin-packages/install-upload")
async def upload_plugin_package(
    request: Request,
    package: UploadFile = File(...),
    sha256: str = Form(...),
    trust_package: bool = Form(False),
    db: Session = Depends(get_db),
) -> dict:
    user = require_admin_api(request, db)
    if not trust_package:
        raise HTTPException(status_code=422, detail="Confirm that you trust the package source.")
    try:
        manifest = await install_uploaded_plugin_for_user(db, user, package, sha256)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "slug": manifest["slug"],
        "name": manifest.get("name", manifest["slug"]),
        "version": manifest.get("version", ""),
        "restart_required": True,
    }


@app.get("/api/plugin-packages/{slug}/settings")
def get_plugin_package_settings(slug: str, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    try:
        package = installed_plugin_package(slug)
        view = plugin_settings_view(db, package)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"slug": slug, "schema": list(package.settings), **view}


@app.put("/api/plugin-packages/{slug}/settings")
def put_plugin_package_settings(
    slug: str,
    payload: PluginSettingsWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = require_admin_api(request, db)
    try:
        view = save_plugin_configuration_for_user(db, user, slug, payload.values)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"slug": slug, **view}


@app.delete("/api/plugin-packages/{slug}")
def uninstall_plugin_api(slug: str, request: Request, db: Session = Depends(get_db)) -> dict:
    user = require_admin_api(request, db)
    try:
        manifest = uninstall_plugin_for_user(db, user, slug)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "slug": slug,
        "version": manifest.get("version", ""),
        "settings_preserved": True,
        "data_preserved": True,
        "rollback_available": True,
        "restart_required": True,
    }


@app.patch("/api/plugin-packages/{slug}")
def update_plugin_package_status(
    slug: str,
    payload: PluginEnabledRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = require_admin_api(request, db)
    status = package_status()
    known_puzzles = {plugin.slug for plugin in list_puzzle_types()}
    known_puzzles |= {
        plugin["slug"] for plugin in status["installed"]
        if plugin.get("package_type", "puzzle") == "puzzle"
    }
    known_puzzles |= {
        plugin["slug"] for plugin in status["official_catalog"]["plugins"]
        if plugin.get("package_type", "puzzle") == "puzzle"
    }
    if slug not in known_puzzles:
        raise HTTPException(status_code=404, detail="Plugin not found")
    disabled = disabled_puzzle_type_slugs(db)
    before = slug not in disabled
    if payload.enabled:
        disabled.discard(slug)
    else:
        disabled.add(slug)
    set_disabled_puzzle_type_slugs(db, sorted(disabled))
    audit(db, user, "plugin", slug, "plugin.enabled" if payload.enabled else "plugin.disabled", {"enabled": before}, {"enabled": payload.enabled})
    db.commit()
    return {"slug": slug, "enabled": payload.enabled}


@app.post("/api/plugin-packages/{slug}/rollback")
def restore_plugin_package_rollback(slug: str, request: Request, db: Session = Depends(get_db)) -> dict:
    user = require_admin_api(request, db)
    try:
        manifest = restore_plugin_for_user(db, user, slug)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "slug": slug,
        "version": manifest.get("version", ""),
        "restart_required": True,
    }


@app.get("/api/puzzles")
def list_puzzles(request: Request, db: Session = Depends(get_db)) -> dict:
    user = require_api_user(request, db)
    library_data = filtered_library(db, request, user, include_archived_for_admin=user.can('library.archived_view'))
    return {
        "items": [
            serialize_library_item(
                puzzle,
                user,
                library_data["previews_by_puzzle"].get(puzzle.id, fallback_preview(puzzle.puzzle_type, puzzle.puzzle_type)),
                library_data["progress_by_puzzle"].get(puzzle.id),
                db,
            )
            for puzzle in library_data["puzzles"]
        ],
        "count": library_data["result_count"],
        "returned": len(library_data["puzzles"]),
        "has_more": library_data["has_more"],
        "next_limit": library_data["next_limit"] if library_data["has_more"] else None,
        "filters": library_data["filters"],
    }


@app.get("/api/puzzles/latest", response_model=PuzzleSummary)
def get_latest_puzzle(request: Request, printable: bool = False, db: Session = Depends(get_db)) -> PuzzleSummary:
    user = require_api_user(request, db)
    puzzles = db.scalars(select(Puzzle).where(Puzzle.archived_at.is_(None)).order_by(desc(Puzzle.created_at))).all()
    for puzzle in puzzles:
        if not can_discover(user,puzzle): continue
        if not puzzle_access_granted(request, puzzle): continue
        try:
            require_series_access(db, puzzle, user)
        except HTTPException as error:
            if error.status_code == 423:
                continue
            raise
        puzzle_type = get_puzzle_type(puzzle.puzzle_type)
        if printable and (puzzle_type is None or not puzzle_type.supports_print or puzzle_type.digital_only):
            continue
        return serialize_puzzle(puzzle, db)
    raise HTTPException(status_code=404, detail="No printable puzzle found" if printable else "No puzzle found")


def printable_puzzle_for_automation(
    puzzle: Puzzle,
    user: User,
    request: Request,
    db: Session,
) -> dict:
    if not can_view(user, puzzle):
        raise HTTPException(status_code=404, detail="Puzzle not found")
    if not puzzle_access_granted(request, puzzle):
        raise HTTPException(status_code=423, detail="Puzzle password required")
    require_series_access(db, puzzle, user)
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if puzzle_type is None or not puzzle_type.supports_print or puzzle_type.digital_only:
        raise HTTPException(status_code=422, detail=f"{puzzle.title} does not support printing")
    render_url = authorized_render_url(puzzle, user, request, db)
    layout = print_layout_context(puzzle, puzzle_type, db)
    layout["render_url"] = render_url
    return {
        "id": puzzle.id,
        "title": puzzle.title,
        "puzzle_type": puzzle.puzzle_type,
        "print_payload": puzzle.print_payload or {},
        "layout": layout,
        "url": puzzle_url(puzzle.id, db),
        "print_url": puzzle_print_url(puzzle.id, db),
        "render_url": render_url,
        "pdf_url": f"{app_base_url(db)}/api/puzzles/{puzzle.id}/print.pdf",
        "png_url": f"{app_base_url(db)}/api/puzzles/{puzzle.id}/print.png",
    }


@app.post("/api/print/manifest")
def create_print_manifest(
    payload: PrintManifestRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = require_api_user(request, db)
    if not (payload.puzzle_ids or payload.issue_ids or payload.latest_puzzle or payload.latest_issue):
        raise HTTPException(status_code=422, detail="Select puzzle IDs, issue IDs, or a latest item")

    puzzles: list[Puzzle] = []
    seen_puzzles: set[str] = set()

    def add_puzzle(puzzle: Puzzle) -> None:
        if puzzle.id not in seen_puzzles:
            seen_puzzles.add(puzzle.id)
            puzzles.append(puzzle)

    for puzzle_id in payload.puzzle_ids:
        puzzle = db.get(Puzzle, puzzle_id)
        if puzzle is None:
            raise HTTPException(status_code=404, detail=f"Puzzle {puzzle_id} not found")
        add_puzzle(puzzle)
    if payload.latest_puzzle:
        candidates = db.scalars(
            select(Puzzle).where(Puzzle.archived_at.is_(None)).order_by(desc(Puzzle.created_at))
        ).all()
        latest = None
        for puzzle in candidates:
            plugin = get_puzzle_type(puzzle.puzzle_type)
            if (
                not can_discover(user, puzzle)
                or not puzzle_access_granted(request, puzzle)
                or plugin is None
                or not plugin.supports_print
                or plugin.digital_only
            ):
                continue
            try:
                require_series_access(db, puzzle, user)
            except HTTPException as error:
                if error.status_code == 423:
                    continue
                raise
            latest = puzzle
            break
        if latest is None:
            raise HTTPException(status_code=404, detail="No printable puzzle found")
        add_puzzle(latest)

    from app.publishing import find_issue, issue_metadata, latest_published_issue, require_issue_access
    issues: list[PublishedIssue] = []
    seen_issues: set[str] = set()

    def add_issue(issue: PublishedIssue) -> None:
        require_issue_access(issue, user)
        if issue.published_at is None:
            raise HTTPException(status_code=422, detail=f"Issue {issue.id} must be published before printing")
        if issue.id not in seen_issues:
            seen_issues.add(issue.id)
            issues.append(issue)

    for issue_id in payload.issue_ids:
        add_issue(find_issue(db, issue_id))
    if payload.latest_issue:
        add_issue(latest_published_issue(db))

    return {
        "schema": "puzzary.print-manifest.v1",
        "generated_at": utcnow().isoformat(),
        "puzzles": [printable_puzzle_for_automation(puzzle, user, request, db) for puzzle in puzzles],
        "issues": [issue_metadata(issue, db) for issue in issues],
    }


@app.get("/api/puzzles/export")
def api_export_puzzles(
    request: Request,
    ids: str = "",
    include_archived: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    user = require_admin_api(request, db)
    puzzles = db.scalars(select(Puzzle).order_by(desc(Puzzle.created_at))).all()
    puzzles = [p for p in puzzles if can_view(user,p)]
    puzzles = [
        p for p in puzzles
        if not p.access_password_hash
        or puzzle_access_granted(request, p)
        or can_owned(user, 'puzzles', 'metadata', p.created_by_id)
    ]
    if ids.strip():
        selected = {item.strip() for item in ids.split(",") if item.strip()}
        puzzles = [puzzle for puzzle in puzzles if puzzle.id in selected]
    if not include_archived:
        puzzles = [puzzle for puzzle in puzzles if puzzle.archived_at is None]
    return portable_puzzle_bundle(puzzles, db)


@app.post("/api/puzzles/import")
def api_import_puzzles(payload: PuzzleImportRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    user = require_admin_api(request, db)
    try:
        items = parse_puzzle_import_items(
            source_text=payload.source_text,
            source_url=payload.source_url,
            api_token=payload.source_api_token,
        )
        return import_portable_puzzles(
            db,
            items,
            creator=user,
            dry_run=payload.dry_run,
            duplicate_policy=payload.duplicate_policy,
            import_ids=payload.import_ids,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/puzzles/manual", response_model=PuzzleSummary)
def api_create_manual_puzzle(payload: PuzzleManualWrite, request: Request, db: Session = Depends(get_db)) -> PuzzleSummary:
    user = require_admin_api(request, db)
    try:
        puzzle = write_manual_puzzle(db, payload, user)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return serialize_puzzle(puzzle, db)


@app.patch("/api/puzzles/{puzzle_id}/manual", response_model=PuzzleSummary)
def api_update_manual_puzzle(
    puzzle_id: str,
    payload: PuzzleManualWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> PuzzleSummary:
    user = require_admin_api(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    try:
        puzzle = write_manual_puzzle(db, payload, user, puzzle)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return serialize_puzzle(puzzle, db)


@app.post("/api/puzzles/{puzzle_id}/publish", response_model=PuzzleSummary)
def api_publish_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> PuzzleSummary:
    """Publish a puzzle immediately through an administrative API client."""
    user = require_admin_api(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    require_owned(user, "puzzles", "publish", puzzle.created_by_id)
    error = manual_puzzle_publish_error(db, puzzle)
    if error:
        raise HTTPException(status_code=422, detail=error)
    publish_puzzle_now(db, user, puzzle)
    db.commit()
    db.refresh(puzzle)
    return serialize_puzzle(puzzle, db)


@app.post("/api/puzzles/{puzzle_id}/archive", response_model=PuzzleSummary)
def api_archive_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> PuzzleSummary:
    """Archive a puzzle through an administrative API client."""
    user = require_admin_api(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    require_owned(user, "puzzles", "archive", puzzle.created_by_id)
    before = {"state": puzzle_publication_state(puzzle)}
    puzzle.archived_at = utcnow()
    audit(db, user, "puzzle", puzzle.id, "puzzle.archived", before, {"state": "archived"})
    db.commit()
    db.refresh(puzzle)
    return serialize_puzzle(puzzle, db)


@app.post("/api/puzzles/{puzzle_id}/unarchive", response_model=PuzzleSummary)
def api_unarchive_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> PuzzleSummary:
    """Restore an archived puzzle through an administrative API client."""
    user = require_admin_api(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    require_owned(user, "puzzles", "archive", puzzle.created_by_id)
    before = {"state": puzzle_publication_state(puzzle)}
    puzzle.archived_at = None
    if puzzle.auto_archive_at is not None and normalize_publish_at(puzzle.auto_archive_at) <= utcnow():
        puzzle.auto_archive_at = None
    after = {"state": puzzle_publication_state(puzzle)}
    audit(db, user, "puzzle", puzzle.id, "puzzle.restored", before, after)
    db.commit()
    db.refresh(puzzle)
    return serialize_puzzle(puzzle, db)


@app.get("/api/puzzles/{puzzle_id}/export")
def api_export_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    return portable_puzzle_item(puzzle, db)


@app.get("/api/puzzles/{puzzle_id}", response_model=PuzzleSummary)
def get_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> PuzzleSummary:
    require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    return serialize_puzzle(puzzle, db)


@app.get("/api/puzzles/{puzzle_id}/print-preview", response_model=PuzzlePrintPreview)
def get_puzzle_print_preview(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> PuzzlePrintPreview:
    user = require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    printable = bool(puzzle_type is not None and puzzle_type.supports_print and not puzzle_type.digital_only)
    if not printable or puzzle_type is None:
        raise HTTPException(status_code=404, detail="This puzzle does not support printing")
    render_url = authorized_render_url(puzzle,user,request,db)
    layout = print_layout_context(puzzle,puzzle_type,db)
    layout['render_url'] = render_url
    return PuzzlePrintPreview(
        id=puzzle.id,
        puzzle_type=puzzle.puzzle_type,
        title=puzzle.title,
        printable=printable,
        layout=layout,
        url=puzzle_url(puzzle.id, db),
        print_url=puzzle_print_url(puzzle.id, db),
        render_url=render_url,
        pdf_url=f"{app_base_url(db)}/api/puzzles/{puzzle.id}/print.pdf",
        png_url=f"{app_base_url(db)}/api/puzzles/{puzzle.id}/print.png",
    )


@app.get("/api/puzzles/{puzzle_id}/progress", response_model=PuzzleProgressResponse)
def get_progress(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> PuzzleProgressResponse:
    user = require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    progress = progress_record_for_user(db, puzzle, user)
    return progress_response(puzzle, user, progress)


@app.get("/api/puzzles/{puzzle_id}/offline-package")
def get_offline_package(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    user = require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(404, "Puzzle not found")
    eligible, reason = offline_puzzle_eligibility(puzzle, user)
    if not eligible:
        raise HTTPException(409, reason)
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if puzzle_type is None or not template_exists(f"puzzle_types/{puzzle.puzzle_type}.html"):
        raise HTTPException(409, "This puzzle does not have an offline-compatible play view.")
    progress = progress_record_for_user(db, puzzle, user)
    play_script = puzzle_type.play_script or ""
    if play_script and not play_script.startswith("/"):
        play_script = f"/static/{play_script}"
    assets = [
        "/static/styles.css?v=55",
        "/static/navigation.js?v=1",
        "/static/pwa.js?v=2",
        "/static/puzzle-actions.js?v=4",
        "/static/offline-puzzles.js?v=2",
        "/static/brand/puzzary-mark.svg?v=1",
    ]
    if play_script:
        assets.append(play_script)
    response = progress_response(puzzle, user, progress)
    return {
        "puzzle_id": puzzle.id,
        "title": puzzle.title,
        "puzzle_type": puzzle.puzzle_type,
        "canonical_url": f"/puzzles/{puzzle.id}",
        "download_url": f"/offline/puzzles/{puzzle.id}",
        "assets": assets,
        "puzzle_version": offline_puzzle_version(puzzle),
        "progress": response.model_dump(),
        "offline_progress_fields": list(puzzle_type.offline_progress_fields),
        "completion_requires_sync": True,
        "offline_validation": puzzle_type.offline_validation,
        "offline_hash_mode": puzzle_type.offline_hash_mode,
    }


@app.post("/api/puzzles/{puzzle_id}/offline-sync")
def sync_offline_progress(
    puzzle_id: str,
    payload: OfflineProgressSync,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(404, "Puzzle not found")
    eligible, reason = offline_puzzle_eligibility(puzzle, user)
    if not eligible:
        raise HTTPException(410, reason)
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if puzzle_type is None:
        raise HTTPException(410, "Puzzle module unavailable.")
    current_version = offline_puzzle_version(puzzle)
    if payload.puzzle_version != current_version:
        raise HTTPException(409, detail={
            "conflict": "puzzle_updated",
            "message": "The puzzle changed while this copy was offline. Use the server copy or download it again.",
            "puzzle_version": current_version,
        })
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    progress = progress_record_for_user(db, puzzle, user, create=True)
    current_revision = progress_revision(progress)
    if not payload.force and payload.base_revision != current_revision:
        server = progress_response(puzzle, user, progress)
        raise HTTPException(409, detail={
            "conflict": "server_changed",
            "message": "Progress changed on another device while this copy was offline.",
            "server_progress": server.model_dump(),
        })
    permitted = {
        field: getattr(payload.progress, field)
        for field in puzzle_type.offline_progress_fields
    }
    normalized_payload = PuzzleProgressUpdate.model_validate(permitted)
    before = deepcopy(progress.progress_payload or {})
    if puzzle_type.offline_progress_normalizer:
        progress.progress_payload = puzzle_type.offline_progress_normalizer(normalized_payload, puzzle.solution_payload)
    elif puzzle_type.progress_merger:
        progress.progress_payload = puzzle_type.progress_merger(normalized_payload, puzzle.solution_payload, before)
    else:
        progress.progress_payload = puzzle_type.progress_normalizer(normalized_payload, puzzle.solution_payload)
    record_progress_transition(db, puzzle, user.id, puzzle_type, before, progress.progress_payload)
    db.commit()
    db.refresh(progress)
    return {
        "status": "synced",
        "puzzle_version": current_version,
        "progress": progress_response(puzzle, user, progress).model_dump(),
    }


@app.post("/api/puzzles/{puzzle_id}/progress", response_model=PuzzleProgressResponse)
def save_progress(
    puzzle_id: str,
    payload: PuzzleProgressUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> PuzzleProgressResponse:
    user = require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if puzzle_type is None:
        raise HTTPException(status_code=400, detail="Unsupported puzzle type")
    if puzzle_type.progress_merger and not getattr(user, "is_guest_link", False):
        db.scalar(select(User).where(User.id == user.id).with_for_update())
    progress = progress_record_for_user(db, puzzle, user, create=True)
    before = deepcopy(progress.progress_payload or {})
    progress.progress_payload = (
        puzzle_type.progress_merger(payload, puzzle.solution_payload, progress.progress_payload or {})
        if puzzle_type.progress_merger else puzzle_type.progress_normalizer(payload, puzzle.solution_payload)
    )
    if not getattr(user, "is_guest_link", False):
        record_progress_transition(db, puzzle, user.id, puzzle_type, before, progress.progress_payload)
    db.commit()
    db.refresh(progress)
    return progress_response(puzzle, user, progress)


@app.post("/api/puzzles/{puzzle_id}/actions")
def play_action(
    puzzle_id: str,
    payload: PuzzlePlayAction,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if puzzle_type is None or puzzle_type.collaboration is None:
        raise HTTPException(status_code=400, detail="This puzzle does not support server-side play actions")
    if not getattr(user, "is_guest_link", False):
        db.scalar(select(User).where(User.id == user.id).with_for_update())
    progress = progress_record_for_user(db, puzzle, user, create=True)
    before = deepcopy(progress.progress_payload or {})
    state = deepcopy(before)
    try:
        if payload.changes:
            state = puzzle_type.collaboration(
                state,
                {"kind": "patch", "changes": payload.changes, "target": "", "_actor": user.id},
                puzzle.solution_payload,
            )
        if payload.kind != "patch":
            state = puzzle_type.collaboration(
                state,
                {"kind": payload.kind, "changes": {}, "target": payload.target, "_actor": user.id},
                puzzle.solution_payload,
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    progress.progress_payload = state
    if not getattr(user, "is_guest_link", False):
        if payload.kind in {"check", "progressive_check", "check_entry"}:
            found, expected = puzzle_type.progress_counter(state, puzzle.solution_payload)
            record_puzzle_event(db, puzzle.id, user.id, "attempt", {
                "is_correct": bool(expected > 0 and found >= expected),
                "found_count": found,
                "expected_count": expected,
                "target": payload.target,
            })
        elif payload.kind in {"hint", "clue"}:
            record_puzzle_event(
                db, puzzle.id, user.id, "hint",
                {"target": payload.target or payload.kind},
            )
        elif payload.kind in {"reveal", "reveal_entry"}:
            record_puzzle_event(
                db, puzzle.id, user.id, "reveal",
                {"target": payload.target or "solution", "scope": "entry" if payload.kind == "reveal_entry" else "solution"},
                once_per_run=True,
            )
        record_progress_transition(db, puzzle, user.id, puzzle_type, before, state)
    db.commit()
    return state


@app.post("/api/puzzles/{puzzle_id}/activity")
def record_puzzle_activity(
    puzzle_id: str,
    payload: PuzzleActivityCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    user = require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    if getattr(user, "is_guest_link", False):
        return {"recorded": False}
    if payload.kind == "hint" and not puzzle.hints:
        raise HTTPException(status_code=400, detail="This puzzle has no general hints")
    event = record_puzzle_event(
        db, puzzle.id, user.id, payload.kind,
        {"target": payload.target or "general"}, once_per_run=True,
    )
    db.commit()
    return {"recorded": event is not None}


@app.post("/api/puzzles/{puzzle_id}/reset", response_model=PuzzleProgressResponse)
def reset_progress(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> PuzzleProgressResponse:
    user = require_api_user(request, db)
    if getattr(user, "is_guest_link", False):
        puzzle = db.get(Puzzle, puzzle_id)
        user.guest_session.progress_payload = {}
        db.commit()
        return progress_response(puzzle, user, user.guest_session)
    puzzle = reset_user_puzzle(puzzle_id, user.id, db)
    return progress_response(puzzle, user, None)


@app.post("/api/puzzles/{puzzle_id}/attempts", response_model=PuzzleAttemptResponse)
def submit_attempt(
    puzzle_id: str,
    payload: PuzzleAttemptCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> PuzzleAttemptResponse:
    user = require_api_user(request, db)
    if getattr(user, "is_guest_link", False):
        if payload.user_id is not None:
            raise HTTPException(403, "Guest links cannot submit for another player")
        puzzle = db.get(Puzzle, puzzle_id)
        if puzzle is None:
            raise HTTPException(404, "Puzzle not found")
        puzzle_type = get_puzzle_type(puzzle.puzzle_type)
        is_correct, expected_count, matched_count = puzzle_type.answer_checker(payload.answer, puzzle.solution_payload)
        return PuzzleAttemptResponse(is_correct=is_correct, message="Correct" if is_correct else "Not yet", expected_count=expected_count, matched_count=matched_count)
    if payload.user_id is not None and payload.user_id != user.id:
        require_permission(user,'puzzles.progress.any')
    user_id = payload.user_id if payload.user_id is not None else user.id
    return submit_puzzle_attempt(puzzle_id, payload.answer, user_id, db)


def submit_puzzle_attempt(
    puzzle_id: str,
    answer: str,
    user_id: int | None,
    db: Session,
) -> PuzzleAttemptResponse:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")

    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if puzzle_type is None:
        raise HTTPException(status_code=400, detail="Unsupported puzzle type")
    is_correct, expected_count, matched_count = puzzle_type.answer_checker(answer, puzzle.solution_payload)

    db.add(
        PuzzleAttempt(
            puzzle_id=puzzle_id,
            user_id=user_id,
            answer=answer,
            is_correct=is_correct,
            attempt_metadata={"matched_count": matched_count, "expected_count": expected_count},
        )
    )
    record_puzzle_event(db, puzzle.id, user_id, "attempt", {
        "is_correct": is_correct,
        "matched_count": matched_count,
        "expected_count": expected_count,
    })
    record_puzzle_event(db, puzzle.id, user_id, "started", once_per_run=True)
    if is_correct:
        record_puzzle_event(
            db, puzzle.id, user_id, "completion",
            {"matched_count": matched_count, "expected_count": expected_count},
            once_per_run=True,
        )
    db.commit()
    return PuzzleAttemptResponse(
        is_correct=is_correct,
        message="Correct." if is_correct else "Not quite. Try again.",
        # Playable puzzle modules use their dedicated server-side actions.
        # Returning partial match counts here creates a generic probing oracle.
        expected_count=None if puzzle_type.supports_web_play else expected_count,
        matched_count=None if puzzle_type.supports_web_play else matched_count,
    )


@app.post("/api/puzzles/{puzzle_id}/photos")
def upload_photo(
    puzzle_id: str,
    request: Request,
    user_id: int | None = Form(None),
    notes: str = Form(""),
    attempt_id: int | None = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict[str, str | int]:
    user = require_api_user(request, db)
    if getattr(user, "is_guest_link", False):
        raise HTTPException(403, "Guest links do not allow photo uploads")
    if user_id is not None and user_id != user.id:
        require_permission(user,'puzzles.progress.any')
    photo_user_id = user_id if user_id is not None else user.id
    return save_photo(puzzle_id, file, notes, photo_user_id, db, attempt_id=attempt_id)


def reset_user_puzzle(puzzle_id: str, user_id: int, db: Session) -> Puzzle:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    plugin = get_puzzle_type(puzzle.puzzle_type)
    if plugin and plugin.progress_merger:
        db.scalar(select(User).where(User.id == user_id).with_for_update())
    progress = db.scalar(select(PuzzleProgress).where(PuzzleProgress.puzzle_id == puzzle_id, PuzzleProgress.user_id == user_id))
    if progress is not None:
        db.delete(progress)
    attempts = db.scalars(select(PuzzleAttempt).where(PuzzleAttempt.puzzle_id == puzzle_id, PuzzleAttempt.user_id == user_id)).all()
    record_puzzle_event(db, puzzle.id, user_id, "reset", {
        "cleared_progress": progress is not None,
        "cleared_attempts": len(attempts),
    })
    for attempt in attempts:
        db.delete(attempt)
    db.commit()
    return puzzle


def save_photo(
    puzzle_id: str,
    file: UploadFile,
    notes: str,
    user_id: int | None,
    db: Session,
    *,
    attempt_id: int | None = None,
) -> dict[str, str | int]:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    if len(notes) > 2000:
        raise HTTPException(status_code=422, detail="Photo notes must be 2,000 characters or fewer")
    attempt = db.get(PuzzleAttempt, attempt_id) if attempt_id is not None else None
    if attempt_id is not None and (
        attempt is None or attempt.puzzle_id != puzzle_id or attempt.user_id != user_id
    ):
        raise HTTPException(status_code=422, detail="The selected attempt does not belong to this puzzle and user")

    original_filename = safe_photo_filename(file.filename)
    first = file.file.read(16)
    media_type = detect_photo_media_type(first)
    if media_type is None:
        raise HTTPException(status_code=415, detail="Upload a JPEG, PNG, or WebP image")
    target_dir = UPLOAD_DIR / puzzle_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex}{PHOTO_TYPES[media_type]}"
    temporary = target.with_suffix(target.suffix + ".upload")
    size = len(first)
    try:
        with temporary.open("xb") as handle:
            handle.write(first)
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > PHOTO_UPLOAD_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="Photo uploads are limited to 10 MB")
                handle.write(chunk)
        os.replace(temporary, target)
        photo = PuzzlePhoto(
            puzzle_id=puzzle_id,
            user_id=user_id,
            attempt_id=attempt_id,
            file_path=str(target),
            original_filename=original_filename,
            notes=notes.strip(),
        )
        db.add(photo)
        db.flush()
        record_puzzle_event(db, puzzle_id, user_id, "photo_upload", {
            "photo_id": photo.id,
            "content_type": media_type,
            "size_bytes": size,
        })
        db.commit()
        db.refresh(photo)
        return {"id": photo.id, "status": "uploaded", "size_bytes": size}
    except Exception:
        db.rollback()
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        remove_empty_photo_directory(puzzle_id)
        raise


def safe_photo_filename(filename: str | None) -> str:
    normalized = (filename or "photo").replace("\x00", "").replace("\\", "/")
    cleaned = Path(normalized).name.strip()
    return (cleaned or "photo")[:255]


def detect_photo_media_type(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def photo_media_type(path: Path) -> str:
    media_types = {suffix: media_type for media_type, suffix in PHOTO_TYPES.items()}
    return media_types.get(path.suffix.lower(), "application/octet-stream")


def stored_photo_path(file_path: str) -> Path:
    root = UPLOAD_DIR.resolve()
    target = Path(file_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="Photo file not found")
    return target


def delete_photo_file(file_path: str) -> None:
    try:
        stored_photo_path(file_path).unlink(missing_ok=True)
    except HTTPException:
        pass


def remove_empty_photo_directory(puzzle_id: str) -> None:
    directory = (UPLOAD_DIR / puzzle_id).resolve()
    if directory.parent != UPLOAD_DIR.resolve():
        return
    try:
        directory.rmdir()
    except (FileNotFoundError, OSError):
        pass
