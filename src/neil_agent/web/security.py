"""Short-lived local bootstrap sessions for the read-only workbench."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hmac import compare_digest

BOOTSTRAP_TTL = timedelta(minutes=2)
SESSION_TTL = timedelta(hours=8)
MAX_SESSIONS = 8


@dataclass(frozen=True, slots=True)
class WebSession:
    token: str
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

    def exchange(self, presented_secret: str | None) -> WebSession | None:
        """Atomically consume the launch secret and issue an opaque session."""

        now = self._now()
        expected = self._bootstrap_secret
        self._bootstrap_secret = None
        if (
            expected is None
            or presented_secret is None
            or now > self._bootstrap_expires_at
            or not compare_digest(expected, presented_secret)
        ):
            return None
        self._purge(now)
        if len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda item: item.expires_at)
            self._sessions.pop(oldest.token, None)
        token = secrets.token_urlsafe(32)
        session = WebSession(token=token, expires_at=now + SESSION_TTL)
        self._sessions[token] = session
        return session

    def validate(self, token: str | None) -> bool:
        """Validate one exact, unexpired session without extending its lifetime."""

        if token is None:
            return False
        now = self._now()
        self._purge(now)
        return any(compare_digest(token, candidate) for candidate in self._sessions)

    def _purge(self, now: datetime) -> None:
        expired = [
            token
            for token, session in self._sessions.items()
            if session.expires_at < now
        ]
        for token in expired:
            self._sessions.pop(token, None)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Web session clock must return timezone-aware timestamps")
        return now
