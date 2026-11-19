from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.models import OIDCProvider


DISCOVERY_CACHE_SECONDS = 300
MAX_RESPONSE_BYTES = 1024 * 1024
SAFE_ID_TOKEN_ALGORITHMS = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"}
_DISCOVERY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


class OIDCError(ValueError):
    """A safe-to-display OIDC configuration or protocol error."""


@dataclass(frozen=True)
class OIDCResult:
    issuer: str
    subject: str
    email: str | None
    email_verified: bool
    display_name: str
    claims: dict[str, Any]


def normalize_issuer(value: str) -> str:
    issuer = value.strip().rstrip("/")
    parsed = urlparse(issuer)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise OIDCError("Issuer must be an HTTP or HTTPS URL without credentials, query, or fragment.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise OIDCError("OIDC issuers must use HTTPS. HTTP is allowed only for local testing.")
    return issuer


def normalize_scopes(value: str) -> str:
    scopes: list[str] = []
    for scope in value.replace(",", " ").split():
        clean = scope.strip()
        if clean and clean not in scopes:
            scopes.append(clean)
    if "openid" not in scopes:
        scopes.insert(0, "openid")
    if len(" ".join(scopes)) > 500:
        raise OIDCError("OIDC scopes are too long.")
    return " ".join(scopes)


def _fernet(secret_key: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(value: str, secret_key: str) -> str:
    if not value:
        return ""
    return _fernet(secret_key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str, secret_key: str) -> str:
    if not value:
        return ""
    try:
        return _fernet(secret_key).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as error:
        raise OIDCError("The saved OIDC secret cannot be decrypted with this server key.") from error


def _read_json(response) -> dict[str, Any]:
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise OIDCError("The identity provider response was too large.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise OIDCError("The identity provider returned an invalid JSON response.") from error
    if not isinstance(payload, dict):
        raise OIDCError("The identity provider returned an unexpected response.")
    return payload


def fetch_json(
    url: str,
    *,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OIDCError("The identity provider advertised an invalid endpoint.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise OIDCError("The identity provider advertised an insecure endpoint.")
    encoded = urlencode(data).encode("utf-8") if data is not None else None
    request = Request(
        url,
        data=encoded,
        headers={
            "Accept": "application/json",
            "User-Agent": "Puzzary/0.1 OIDC client",
            **(headers or {}),
        },
    )
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is admin configured and validated
            final_url = urlparse(response.geturl())
            if final_url.scheme not in {"http", "https"} or not final_url.netloc:
                raise OIDCError("The identity provider redirected to an invalid endpoint.")
            if final_url.scheme != "https" and final_url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise OIDCError("The identity provider redirected to an insecure endpoint.")
            return _read_json(response)
    except HTTPError as error:
        detail = ""
        try:
            payload = _read_json(error)
            detail = str(payload.get("error_description") or payload.get("error") or "")[:160]
        except OIDCError:
            pass
        suffix = f" ({detail})" if detail else ""
        raise OIDCError(f"The identity provider rejected the request{suffix}.") from error
    except (URLError, TimeoutError, OSError) as error:
        raise OIDCError("The identity provider could not be reached.") from error


def provider_metadata(provider: OIDCProvider, *, refresh: bool = False) -> dict[str, Any]:
    issuer = normalize_issuer(provider.issuer_url)
    cached = _DISCOVERY_CACHE.get(issuer)
    if not refresh and cached and time.monotonic() - cached[0] < DISCOVERY_CACHE_SECONDS:
        return cached[1]
    metadata = fetch_json(f"{issuer}/.well-known/openid-configuration")
    discovered_issuer = str(metadata.get("issuer", ""))
    if normalize_issuer(discovered_issuer) != issuer:
        raise OIDCError("The discovery document issuer does not match the configured issuer.")
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        endpoint = str(metadata.get(field, ""))
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise OIDCError(f"The discovery document is missing a valid {field}.")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise OIDCError(f"The discovery document {field} must use HTTPS.")
    response_types = metadata.get("response_types_supported", [])
    if response_types and "code" not in response_types:
        raise OIDCError("The identity provider does not advertise authorization code flow support.")
    challenge_methods = metadata.get("code_challenge_methods_supported", [])
    if challenge_methods and "S256" not in challenge_methods:
        raise OIDCError("The identity provider does not advertise PKCE S256 support.")
    _DISCOVERY_CACHE[issuer] = (time.monotonic(), metadata)
    return metadata


def new_authorization_request(
    provider: OIDCProvider,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_verifier: str,
) -> str:
    metadata = provider_metadata(provider)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    query = urlencode(
        {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "scope": normalize_scopes(provider.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{metadata['authorization_endpoint']}?{query}"


def _token_request(
    provider: OIDCProvider,
    metadata: dict[str, Any],
    client_secret: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> dict[str, Any]:
    basic = base64.b64encode(f"{provider.client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    return fetch_json(
        str(metadata["token_endpoint"]),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        headers={"Authorization": f"Basic {basic}"},
    )


def _validated_claims(
    provider: OIDCProvider,
    metadata: dict[str, Any],
    id_token: str,
    nonce: str,
) -> dict[str, Any]:
    header = jwt.get_unverified_header(id_token)
    algorithm = str(header.get("alg", ""))
    advertised = set(metadata.get("id_token_signing_alg_values_supported", []))
    allowed = SAFE_ID_TOKEN_ALGORITHMS & (advertised or SAFE_ID_TOKEN_ALGORITHMS)
    if algorithm not in allowed:
        raise OIDCError("The identity provider used an unsupported ID token algorithm.")
    jwks = fetch_json(str(metadata["jwks_uri"]))
    try:
        key_set = jwt.PyJWKSet.from_dict(jwks)
        key_id = str(header.get("kid", ""))
        signing_key = next(
            key.key for key in key_set.keys if not key_id or key.key_id == key_id
        )
        claims = jwt.decode(
            id_token,
            signing_key,
            algorithms=[algorithm],
            audience=provider.client_id,
            issuer=str(metadata["issuer"]),
            leeway=30,
            options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
        )
    except (StopIteration, jwt.PyJWTError, ValueError, TypeError) as error:
        raise OIDCError("The identity provider ID token could not be validated.") from error
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OIDCError("The identity provider response nonce did not match.")
    return claims


def complete_authorization(
    provider: OIDCProvider,
    client_secret: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
    nonce: str,
) -> OIDCResult:
    metadata = provider_metadata(provider)
    token_response = _token_request(
        provider, metadata, client_secret, redirect_uri, code, code_verifier
    )
    id_token = str(token_response.get("id_token", ""))
    if not id_token:
        raise OIDCError("The identity provider did not return an ID token.")
    claims = _validated_claims(provider, metadata, id_token, nonce)
    access_token = str(token_response.get("access_token", ""))
    userinfo_endpoint = str(metadata.get("userinfo_endpoint", ""))
    if access_token and userinfo_endpoint:
        userinfo = fetch_json(
            userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if str(userinfo.get("sub", "")) != str(claims.get("sub", "")):
            raise OIDCError("The identity provider user information did not match the ID token.")
        claims = {**claims, **userinfo}
    subject = str(claims.get("sub", "")).strip()
    if not subject:
        raise OIDCError("The identity provider did not return a stable subject identifier.")
    email = str(claims.get("email", "")).strip().lower() or None
    email_verified = claims.get("email_verified") is True
    display_name = str(
        claims.get("name") or claims.get("preferred_username") or email or "OIDC user"
    ).strip()[:160]
    retained = {
        key: claims[key]
        for key in ("sub", "email", "email_verified", "name", "preferred_username")
        if key in claims
    }
    return OIDCResult(
        issuer=normalize_issuer(str(metadata["issuer"])),
        subject=subject,
        email=email,
        email_verified=email_verified,
        display_name=display_name,
        claims=retained,
    )


def provider_summary(provider: OIDCProvider, callback_url: str) -> dict[str, Any]:
    return {
        "id": provider.id,
        "display_name": provider.display_name,
        "issuer_url": provider.issuer_url,
        "client_id": provider.client_id,
        "has_client_secret": bool(provider.client_secret_encrypted),
        "scopes": provider.scopes,
        "enabled": provider.enabled,
        "provisioning_mode": provider.provisioning_mode,
        "last_tested_at": provider.last_tested_at.isoformat() if provider.last_tested_at else None,
        "last_test_status": provider.last_test_status,
        "last_test_message": provider.last_test_message,
        "callback_url": callback_url,
    }
