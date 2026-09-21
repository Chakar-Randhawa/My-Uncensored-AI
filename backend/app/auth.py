"""
Supabase JWT verification.

Supabase now issues session JWTs two different ways depending on the
project's Auth configuration (Project Settings -> JWT Signing Keys):

  * **Asymmetric (current default for new projects, since Oct 2025)** —
    signed with ES256 or RS256. Verified against a public key fetched
    from the project's JWKS endpoint
    (`https://<ref>.supabase.co/auth/v1/.well-known/jwks.json`). This is
    the primary path here: `SUPABASE_JWKS_URL` is required.
  * **Legacy (symmetric HS256)** — signed with a single shared secret
    (`SUPABASE_JWT_SECRET`, Project Settings -> API -> JWT Secret). Older
    projects that haven't migrated may still issue these. Supported here
    as a fallback *only* when `SUPABASE_JWT_SECRET` is set — most new
    projects won't have one and don't need it.

Either way, verification happens locally (no network round-trip to
Supabase Auth per request) — JWKS keys are fetched once and cached by
`PyJWKClient`, which matters when every millisecond of latency competes
with the racing engine's own budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError

from app.config import Settings, get_settings

_bearer_scheme = HTTPBearer(auto_error=False)

# PyJWKClient does its own internal caching of fetched keys (keyed by JWKS
# URL), but caching the *client* instance itself avoids re-parsing the
# JWKS URL and rebuilding the client's internal state on every request.
_jwks_client_cache: dict[str, PyJWKClient] = {}


@dataclass(slots=True, frozen=True)
class AuthenticatedUser:
    id: UUID
    email: str | None
    role: str


def _get_jwks_client(jwks_url: str) -> PyJWKClient:
    client = _jwks_client_cache.get(jwks_url)
    if client is None:
        # lifespan=3600: re-fetch the JWKS at most once an hour. Supabase
        # keeps a previous key available for a grace period after
        # rotation specifically so in-flight verification like this
        # doesn't break mid-rotation.
        client = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
        _jwks_client_cache[jwks_url] = client
    return client


def _decode_supabase_jwt(token: str, settings: Settings) -> dict:
    try:
        if settings.supabase_jwks_url:
            jwks_client = _get_jwks_client(settings.supabase_jwks_url)
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                # Supabase's asymmetric keys are ES256 or RS256 depending
                # on which the project chose when it migrated — accepting
                # both here means this doesn't need to change if the
                # project later rotates from one to the other.
                algorithms=["ES256", "RS256"],
                audience="authenticated",
                options={"require": ["exp", "sub"]},
            )

        if settings.supabase_jwt_secret:
            return jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
                options={"require": ["exp", "sub"]},
            )

        # Misconfiguration, not a client error — fail loudly rather than
        # silently rejecting every request with a generic 401.
        raise RuntimeError(
            "No JWT verification method configured: set SUPABASE_JWKS_URL "
            "(current Supabase projects) or SUPABASE_JWT_SECRET (legacy "
            "HS256 projects)."
        )

    except PyJWKClientError as exc:
        # JWKS fetch failed, or the token's `kid` doesn't match any known
        # key — treat as an auth failure, not a 500, since an attacker
        # sending a garbage `kid` shouldn't be distinguishable from any
        # other invalid token.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not verify token signature.",
        ) from exc
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired — please sign in again.",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
        ) from exc


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = _decode_supabase_jwt(credentials.credentials, settings)

    try:
        user_id = UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing a valid subject claim.",
        ) from exc

    return AuthenticatedUser(
        id=user_id,
        email=claims.get("email"),
        role=claims.get("role", "authenticated"),
    )
