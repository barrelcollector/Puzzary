"""Validated HTTP, native TLS, and trusted reverse-proxy configuration."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import ssl
from urllib.parse import urlsplit


class TransportConfigurationError(ValueError):
    pass


def _boolean(env: dict[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise TransportConfigurationError(f"{name} must be true or false")


def _trusted_proxies(raw: str) -> tuple[str, ...]:
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    for value in values:
        if value == "*":
            continue
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError as error:
            raise TransportConfigurationError(
                f"PUZZARY_TRUSTED_PROXIES contains invalid IP address or CIDR: {value}"
            ) from error
    return values


@dataclass(frozen=True)
class TransportConfig:
    public_base_url: str
    tls_cert_file: str
    tls_key_file: str
    tls_key_password: str
    trusted_proxies: tuple[str, ...]
    https_redirect: bool
    secure_cookies: bool
    host: str
    port: int
    warnings: tuple[str, ...]

    @property
    def native_tls(self) -> bool:
        return bool(self.tls_cert_file and self.tls_key_file)

    @property
    def forwarded_allow_ips(self) -> str | None:
        return ",".join(self.trusted_proxies) if self.trusted_proxies else None

    def validate_native_tls_files(self) -> None:
        if not self.native_tls:
            return
        for label, value in (
            ("PUZZARY_TLS_CERT_FILE", self.tls_cert_file),
            ("PUZZARY_TLS_KEY_FILE", self.tls_key_file),
        ):
            path = Path(value)
            if not path.is_file():
                raise TransportConfigurationError(f"{label} does not reference a readable file: {value}")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(
                self.tls_cert_file,
                self.tls_key_file,
                password=self.tls_key_password or None,
            )
        except (OSError, ssl.SSLError) as error:
            raise TransportConfigurationError(
                "Puzzary could not load the configured TLS certificate and private key; "
                "verify the PEM files, matching key, and key password"
            ) from error


def load_transport_config(
    env: dict[str, str] | None = None,
    *,
    validate_files: bool = False,
) -> TransportConfig:
    values = dict(os.environ if env is None else env)
    public_base_url = values.get("APP_BASE_URL", "http://localhost:8790").strip().rstrip("/")
    parsed = urlsplit(public_base_url)
    try:
        parsed_port = parsed.port
    except ValueError as error:
        raise TransportConfigurationError("APP_BASE_URL contains an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
        or (parsed_port is not None and not 1 <= parsed_port <= 65535)
    ):
        raise TransportConfigurationError("APP_BASE_URL must be an absolute HTTP or HTTPS origin without credentials, query, or fragment")

    cert_file = values.get("PUZZARY_TLS_CERT_FILE", "").strip()
    key_file = values.get("PUZZARY_TLS_KEY_FILE", "").strip()
    if bool(cert_file) != bool(key_file):
        raise TransportConfigurationError(
            "PUZZARY_TLS_CERT_FILE and PUZZARY_TLS_KEY_FILE must be configured together"
        )
    native_tls = bool(cert_file and key_file)
    if native_tls and parsed.scheme != "https":
        raise TransportConfigurationError("APP_BASE_URL must use https:// when native TLS is enabled")

    trusted_proxies = _trusted_proxies(values.get("PUZZARY_TRUSTED_PROXIES", ""))
    https_redirect = _boolean(values, "PUZZARY_HTTPS_REDIRECT")
    if https_redirect and parsed.scheme != "https":
        raise TransportConfigurationError("PUZZARY_HTTPS_REDIRECT requires an https:// APP_BASE_URL")
    if https_redirect and not native_tls and not trusted_proxies:
        raise TransportConfigurationError(
            "PUZZARY_HTTPS_REDIRECT behind a reverse proxy requires PUZZARY_TRUSTED_PROXIES"
        )

    cookie_setting = values.get("PUZZARY_SECURE_COOKIES", "auto").strip().lower() or "auto"
    if cookie_setting == "auto":
        secure_cookies = parsed.scheme == "https" or native_tls
    elif cookie_setting in {"1", "true", "yes", "on"}:
        secure_cookies = True
    elif cookie_setting in {"0", "false", "no", "off"}:
        secure_cookies = False
    else:
        raise TransportConfigurationError("PUZZARY_SECURE_COOKIES must be auto, true, or false")
    if parsed.scheme == "https" and not secure_cookies:
        raise TransportConfigurationError("Secure session cookies cannot be disabled for an HTTPS APP_BASE_URL")

    try:
        port = int(values.get("PUZZARY_PORT", "8790"))
    except ValueError as error:
        raise TransportConfigurationError("PUZZARY_PORT must be an integer from 1 to 65535") from error
    if not 1 <= port <= 65535:
        raise TransportConfigurationError("PUZZARY_PORT must be an integer from 1 to 65535")

    warnings: list[str] = []
    if parsed.scheme == "http":
        warnings.append(
            "Public base URL uses HTTP. Passwords, API tokens, guest links, and session cookies must not cross an untrusted network."
        )
    if secure_cookies and parsed.scheme != "https":
        warnings.append("Secure cookies are enabled while APP_BASE_URL uses HTTP; browser login will require HTTPS.")
    if "*" in trusted_proxies:
        warnings.append("All clients are trusted as proxies; restrict PUZZARY_TRUSTED_PROXIES to proxy IPs or CIDRs.")

    config = TransportConfig(
        public_base_url=public_base_url,
        tls_cert_file=cert_file,
        tls_key_file=key_file,
        tls_key_password=values.get("PUZZARY_TLS_KEY_PASSWORD", ""),
        trusted_proxies=trusted_proxies,
        https_redirect=https_redirect,
        secure_cookies=secure_cookies,
        host=values.get("PUZZARY_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=port,
        warnings=tuple(warnings),
    )
    if validate_files:
        config.validate_native_tls_files()
    return config
