"""Bearer-token authentication (operator decision 2026-10-13).

One token, from ``ARBITRON_API_TOKEN`` (or the ``arbitron_api_token`` file in
``ARBITRON_SECRETS_DIR``), guards every endpoint except the minimal ``/health``
liveness probe. Comparison is constant-time; the token is never logged.
"""

import hmac
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Header, HTTPException, status

MIN_TOKEN_LENGTH = 32


class ApiTokenError(ValueError):
    """The configured API token is missing or too weak."""


def validate_token(token: str | None) -> str:
    if token is None:
        msg = "ARBITRON_API_TOKEN is not set; the operational API refuses to start without it"
        raise ApiTokenError(msg)
    if len(token) < MIN_TOKEN_LENGTH or token != token.strip():
        msg = (
            f"ARBITRON_API_TOKEN must be at least {MIN_TOKEN_LENGTH} characters with no "
            "surrounding whitespace (e.g. `openssl rand -hex 32`)"
        )
        raise ApiTokenError(msg)
    return token


def bearer_auth(
    token: str, *, on_failure: Callable[[], None] | None = None
) -> Callable[[str | None], Awaitable[None]]:
    """A FastAPI dependency that rejects requests without the bearer token."""
    expected = validate_token(token).encode()

    async def require(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, presented = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            presented.strip().encode(), expected
        ):
            if on_failure is not None:
                on_failure()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require
