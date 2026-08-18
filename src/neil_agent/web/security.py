"""Short-lived local sessions and one-time WebSocket tickets."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from threading import RLock

BOOTSTRAP_TTL = timedelta(minutes=2)
SESSION_TTL = timedelta(hours=8)
WS_TICKET_TTL = timedelta(seconds=30)
MAX_SESSIONS = 8
MAX_WS_TICKETS = 16


@dataclass(frozen=True, slots=True)
class WebSession:
    token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class WebSocketTicket:
    token: str
    session_token: str
    expires_at: datetime


class BootstrapSessionStore:
    """Consume one launch secret and maintain a small in-memory session set."""

    def __init__(
        self,
        bootstrap_secret: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if len(bootstrap_secret) < 32:
            raise ValueError(
                "Web Workbench bootstrap secret must be at least 32 characters"
            )
        self._bootstrap_secret: str | None = bootstrap_secret
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._bootstrap_expires_at = self._now() + BOOTSTRAP_TTL
        self._sessions: dict[str, WebSession] = {}
        self._ws_tickets: dict[str, WebSocketTicket] = {}
        self._lock = RLock()

    def exchange(self, presented_secret: str | None) -> WebSession | None:
        """Atomically consume the launch secret and issue an opaque session."""

        with self._lock:
            now = self._now()
            expected = self._bootstrap_secret
            self._bootstrap_secret = None
            if (
                expected is None
                or presented_secret is None
                or now >= self._bootstrap_expires_at
                or not compare_digest(expected, presented_secret)
            ):
                return None
            self._purge(now)
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda item: item.expires_at)
                self._sessions.pop(oldest.token, None)
            token = secrets.token_urlsafe(32)
            session = WebSession(
                token=token,
                csrf_token=secrets.token_urlsafe(32),
                expires_at=now + SESSION_TTL,
            )
            self._sessions[token] = session
            return session

    def validate(self, token: str | None) -> bool:
        """Validate one exact, unexpired session without extending its lifetime."""

        with self._lock:
            if token is None:
                return False
            now = self._now()
            self._purge(now)
            return self._find_session(token) is not None

    def validate_csrf(
        self,
        session_token: str | None,
        presented_csrf: str | None,
    ) -> bool:
        """Bind one double-submit CSRF value to an exact local session."""

        with self._lock:
            if session_token is None or presented_csrf is None:
                return False
            now = self._now()
            self._purge(now)
            session = self._find_session(session_token)
            return session is not None and compare_digest(
                session.csrf_token, presented_csrf
            )

    def issue_ws_ticket(self, session_token: str | None) -> WebSocketTicket | None:
        """Issue one bounded, short-lived ticket for an authenticated session."""

        with self._lock:
            if session_token is None:
                return None
            now = self._now()
            self._purge(now)
            if self._find_session(session_token) is None:
                return None
            if len(self._ws_tickets) >= MAX_WS_TICKETS:
                oldest = min(
                    self._ws_tickets.values(), key=lambda item: item.expires_at
                )
                self._ws_tickets.pop(oldest.token, None)
            token = secrets.token_urlsafe(32)
            ticket = WebSocketTicket(
                token=token,
                session_token=session_token,
                expires_at=now + WS_TICKET_TTL,
            )
            self._ws_tickets[token] = ticket
            return ticket

    def consume_ws_ticket(self, presented_ticket: str | None) -> bool:
        """Consume an exact ticket once and revalidate its parent session."""

        with self._lock:
            if presented_ticket is None:
                return False
            now = self._now()
            self._purge(now)
            match = next(
                (
                    token
                    for token in self._ws_tickets
                    if compare_digest(token, presented_ticket)
                ),
                None,
            )
            if match is None:
                return False
            ticket = self._ws_tickets.pop(match)
            return (
                ticket.expires_at > now
                and self._find_session(ticket.session_token) is not None
            )

    def _purge(self, now: datetime) -> None:
        expired = [
            token
            for token, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for token in expired:
            self._sessions.pop(token, None)
        expired_tickets = [
            token
            for token, ticket in self._ws_tickets.items()
            if ticket.expires_at <= now or ticket.session_token not in self._sessions
        ]
        for token in expired_tickets:
            self._ws_tickets.pop(token, None)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Web session clock must return timezone-aware timestamps")
        return now

    def _find_session(self, presented_token: str) -> WebSession | None:
        return next(
            (
                session
                for token, session in self._sessions.items()
                if compare_digest(token, presented_token)
            ),
            None,
        )
