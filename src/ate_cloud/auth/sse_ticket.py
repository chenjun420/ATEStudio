"""One-time SSE ticket store and FastAPI dependency (RH-3, v41-remaining-hardening #3).

Native ``EventSource`` cannot send an ``Authorization`` header, so SSE
endpoints cannot ride the central bearer-token mount guard. Instead, an
authenticated client first POSTs ``/api/v1/auth/sse-ticket`` (JWT-protected)
to obtain a short-lived one-time ticket, then opens the EventSource with
``?ticket=<value>`` as a query parameter.

Semantics:
    - Ticket is ``secrets.token_urlsafe(32)`` (256-bit random, stdlib only).
    - TTL: 60 seconds from issue (enough to open an EventSource, useless for
      replay). Validation compares against wall-clock time; the ticket is
      deleted (consumed) on the *first* validation attempt regardless of
      outcome, so a leaked ticket is single-use.
    - Store: module-level dict + threading.Lock. Expired entries are purged
      opportunistically on each issue/validate call - no background task.
    - The consuming user is resolved from the user_id captured at issue
      time; a fresh DB lookup guarantees the user still exists and is
      active (equivalent to ``get_current_user`` semantics minus the
      bearer-token decode).

Dev-mode parity: when ``settings.dev_mode`` is true and no ticket is
supplied, the dependency falls back to the synthetic admin user exactly
like ``get_current_user`` does - keeping the offline test-suite's dev-mode
bypass behavior intact.
"""

import secrets
import threading
import time

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.auth.dependencies import _bearer_scheme, _dev_user
from ate_cloud.config import settings
from ate_cloud.db import get_db
from ate_cloud.models.user import User

# Ticket lifetime in seconds (plan: TTL 60s).
SSE_TICKET_TTL_SECONDS = 60.0

# Module-level one-time ticket store: ticket -> (user_id, expires_at_monotonic-ish).
# Uses wall-clock epoch seconds (time.time()) so expired tickets injected by
# tests are honored; a monotonic clock would make manual expiry testing flaky.
_tickets: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def _purge_expired(now: float) -> None:
    """Drop expired entries (caller holds the lock)."""
    expired = [t for t, (_uid, exp) in _tickets.items() if exp <= now]
    for t in expired:
        del _tickets[t]


def issue_sse_ticket(user_id: str, ttl: float = SSE_TICKET_TTL_SECONDS) -> str:
    """Create and store a new one-time SSE ticket for ``user_id``.

    Args:
        user_id: The authenticated user the ticket will authenticate.
        ttl: Ticket lifetime in seconds (default 60).

    Returns:
        The opaque ticket string (urlsafe random, 256 bits).
    """
    ticket = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        _purge_expired(now)
        _tickets[ticket] = (user_id, now + ttl)
    return ticket


def consume_sse_ticket(ticket: str) -> str | None:
    """Validate and consume a ticket (single-use).

    The ticket is deleted on the FIRST call regardless of validity, so a
    failed check (expired / unknown) can never be retried successfully.

    Args:
        ticket: The ticket string from the query parameter.

    Returns:
        The ``user_id`` bound at issue time, or ``None`` if the ticket is
        unknown or expired.
    """
    now = time.time()
    with _lock:
        _purge_expired(now)
        entry = _tickets.pop(ticket, None)
    if entry is None:
        return None
    user_id, expires_at = entry
    if expires_at <= now:
        return None
    return user_id


def reset_ticket_store() -> None:
    """Clear all outstanding tickets (test isolation helper)."""
    with _lock:
        _tickets.clear()


async def require_sse_user(
    ticket: str = Query(default="", description="One-time SSE ticket from POST /auth/sse-ticket"),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI dependency authenticating an SSE request via one-time ticket.

    Mirrors ``get_current_user`` semantics: resolves a live, active User
    from the ticket's bound user_id. Falls back to the dev-mode synthetic
    admin when ``settings.dev_mode`` is true and no ticket is presented
    (offline/dev parity with the bearer-token path).

    The ticket parameter defaults to an empty string (not ``Query(...)``)
    so a missing ticket yields a semantic 401 rather than FastAPI's 422
    validation error - SSE endpoints must answer 401 for anonymous
    requests (test_auth_enforcement contract).

    Args:
        ticket: One-time ticket from the ``?ticket=`` query parameter.
        credentials: Bearer credentials (dev-mode detection only; a
            browser EventSource never sends them).
        db: Database session for the user lookup.

    Returns:
        The authenticated User.

    Raises:
        HTTPException: 401 if the ticket is missing, invalid, expired,
            already consumed, or the bound user no longer exists/is active.
    """
    if not ticket and settings.dev_mode and credentials is None:
        return _dev_user()

    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing SSE ticket (POST /api/v1/auth/sse-ticket with a bearer token first)",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = consume_sse_ticket(ticket)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired SSE ticket",
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


def ticket_store_size() -> int:
    """Return the number of outstanding (unconsumed, unexpired-purge-pending) tickets."""
    with _lock:
        return len(_tickets)


__all__: list[str] = [
    "SSE_TICKET_TTL_SECONDS",
    "consume_sse_ticket",
    "issue_sse_ticket",
    "require_sse_user",
    "reset_ticket_store",
    "ticket_store_size",
]
