from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

import jwt
from jwt import PyJWKClient


@dataclass(frozen=True)
class AccessIdentity:
    subject: str
    email: str


@dataclass(frozen=True, repr=False)
class CloudWebSettings:
    database_url: str
    r2_endpoint_url: str
    r2_bucket: str
    r2_access_key_id: str
    r2_secret_access_key: str
    access_team_domain: str
    access_audience: str
    csrf_secret: str
    allowed_emails: frozenset[str]
    retention_hours: int = 24
    signed_url_ttl_seconds: int = 900

    def __post_init__(self) -> None:
        if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        if not self.r2_endpoint_url.startswith("https://"):
            raise ValueError("R2_ENDPOINT_URL must use HTTPS")
        if not self.r2_bucket.strip():
            raise ValueError("R2_BUCKET is required")
        if not self.r2_access_key_id.strip() or not self.r2_secret_access_key.strip():
            raise ValueError("R2 credentials are required")
        if not self.access_team_domain.startswith("https://"):
            raise ValueError("CLOUDFLARE_ACCESS_TEAM_DOMAIN must be an HTTPS URL")
        if not self.access_audience.strip():
            raise ValueError("CLOUDFLARE_ACCESS_AUD is required")
        if len(self.csrf_secret) < 32:
            raise ValueError("AUDIO_ARCHIVE_CSRF_SECRET must contain at least 32 characters")
        if not self.allowed_emails:
            raise ValueError("AUDIO_ARCHIVE_ALLOWED_EMAILS must contain at least one email")
        if self.retention_hours <= 0:
            raise ValueError("AUDIO_ARCHIVE_RETENTION_HOURS must be positive")
        if not 60 <= self.signed_url_ttl_seconds <= 3600:
            raise ValueError("AUDIO_ARCHIVE_SIGNED_URL_TTL_SECONDS must be between 60 and 3600")

    @property
    def certs_url(self) -> str:
        return f"{self.access_team_domain.rstrip('/')}/cdn-cgi/access/certs"

    @classmethod
    def from_env(cls) -> "CloudWebSettings":
        team_domain = _required("CLOUDFLARE_ACCESS_TEAM_DOMAIN").rstrip("/")
        allowed = frozenset(
            item.strip().casefold()
            for item in _required("AUDIO_ARCHIVE_ALLOWED_EMAILS").split(",")
            if item.strip()
        )
        return cls(
            database_url=_required("DATABASE_URL"),
            r2_endpoint_url=_required("R2_ENDPOINT_URL"),
            r2_bucket=_required("R2_BUCKET"),
            r2_access_key_id=_required("R2_ACCESS_KEY_ID"),
            r2_secret_access_key=_required("R2_SECRET_ACCESS_KEY"),
            access_team_domain=team_domain,
            access_audience=_required("CLOUDFLARE_ACCESS_AUD"),
            csrf_secret=_required("AUDIO_ARCHIVE_CSRF_SECRET"),
            allowed_emails=allowed,
            retention_hours=_positive_int("AUDIO_ARCHIVE_RETENTION_HOURS", 24),
            signed_url_ttl_seconds=_positive_int(
                "AUDIO_ARCHIVE_SIGNED_URL_TTL_SECONDS", 900
            ),
        )

    def __repr__(self) -> str:
        return (
            "CloudWebSettings("
            "database_url='***', "
            f"r2_endpoint_url={self.r2_endpoint_url!r}, "
            f"r2_bucket={self.r2_bucket!r}, "
            "r2_access_key_id='***', r2_secret_access_key='***', "
            f"access_team_domain={self.access_team_domain!r}, "
            f"access_audience={self.access_audience!r}, "
            "csrf_secret='***', "
            f"allowed_emails={sorted(self.allowed_emails)!r}, "
            f"retention_hours={self.retention_hours}, "
            f"signed_url_ttl_seconds={self.signed_url_ttl_seconds})"
        )


class AccessVerifier(Protocol):
    def verify(self, assertion: str) -> AccessIdentity: ...


class CloudflareAccessVerifier:
    """Validate the signed Access application JWT at the application origin."""

    def __init__(self, settings: CloudWebSettings) -> None:
        self.settings = settings
        self._jwks = PyJWKClient(settings.certs_url, cache_jwk_set=True, lifespan=300)

    def verify(self, assertion: str) -> AccessIdentity:
        if not assertion.strip():
            raise PermissionError("Missing Cloudflare Access assertion")
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(assertion)
            payload = jwt.decode(
                assertion,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.settings.access_audience,
                issuer=self.settings.access_team_domain,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise PermissionError("Invalid Cloudflare Access assertion") from exc
        if payload.get("type") != "app":
            raise PermissionError("Cloudflare Access token is not an application token")
        subject = str(payload.get("sub") or "").strip()
        email = str(payload.get("email") or "").strip().casefold()
        if not subject or not email:
            raise PermissionError("Cloudflare Access token has no verified identity")
        if email not in self.settings.allowed_emails:
            raise PermissionError("Authenticated identity is not authorized for Audio Archive")
        return AccessIdentity(subject=subject, email=email)


class CsrfSigner:
    """Stateless CSRF token bound to the authenticated Access subject."""

    def __init__(self, secret: str, *, max_age_seconds: int = 8 * 60 * 60) -> None:
        if len(secret) < 32:
            raise ValueError("CSRF secret must contain at least 32 characters")
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        self._secret = secret.encode("utf-8")
        self.max_age_seconds = max_age_seconds

    def issue(self, identity: AccessIdentity) -> str:
        issued = int(time.time())
        nonce = secrets.token_urlsafe(24)
        payload = f"{identity.subject}:{issued}:{nonce}"
        signature = hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{issued}.{nonce}.{signature}"

    def verify(self, token: str, identity: AccessIdentity) -> None:
        try:
            issued_text, nonce, supplied = token.split(".", 2)
            issued = int(issued_text)
        except (TypeError, ValueError) as exc:
            raise PermissionError("Invalid CSRF token") from exc
        now = int(time.time())
        if issued > now + 60 or now - issued > self.max_age_seconds:
            raise PermissionError("Expired CSRF token")
        payload = f"{identity.subject}:{issued}:{nonce}"
        expected = hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise PermissionError("Invalid CSRF token")


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value
