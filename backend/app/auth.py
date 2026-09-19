"""
Supabase JWT verification.

The Next.js frontend attaches the user's Supabase session access token as a
standard `Authorization: Bearer <jwt>` header (see
`frontend/lib/supabase/server.ts`). We verify it locally against the
project's JWT secret — no network round-trip to Supabase Auth per request,
which matters when every millisecond of latency competes with the racing
engine's own budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(slots=True, frozen=True)
class AuthenticatedUser:
    id: UUID
    email: str | None
    role: str


def _decode_supabase_jwt(token: str, settings: Settings) -> dict:
    try:
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
            options={"require": ["exp", "sub"]},
        )
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
