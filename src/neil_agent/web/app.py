"""FastAPI application factory for the local realtime workbench."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..config import Settings
from .assets import verify_static_bundle
from .controller import ClientCommand, CommandError, WorkbenchController
from .dto import (
    FileTreeDto,
    GitDiffDto,
    HealthDto,
    ReviewDto,
    SessionListDto,
    WebSocketTicketDto,
    WorkbenchSnapshotDto,
)
from .security import BootstrapSessionStore
from .service import WorkbenchSnapshotService

DEFAULT_ALLOWED_ORIGINS = frozenset(
    {
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8765",
        "http://localhost:8765",
    }
)
TRUSTED_HOSTS = frozenset({"127.0.0.1", "localhost", "testserver"})
MAX_WEBSOCKET_MESSAGE_BYTES = 64 * 1024
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; base-uri 'none'; connect-src 'self'; "
    "font-src 'self'; img-src 'self' data:; manifest-src 'self'; "
    "script-src 'self'; style-src 'self'; frame-ancestors 'none'; "
    "form-action 'none'; object-src 'none'; worker-src 'none'"
)


def loopback_origins(port: int) -> frozenset[str]:
    """Return exact HTTP origins accepted by one loopback listener."""

    if not 1 <= port <= 65_535:
        raise ValueError("loopback origin port must be between 1 and 65535")
    return frozenset({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})


def _validated_origins(origins: Collection[str]) -> frozenset[str]:
    accepted = frozenset(origins)
    if not accepted:
        raise ValueError("Web Workbench must allow at least one loopback origin")
    for origin in accepted:
        parsed = urlsplit(origin)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError(
                "Web Workbench origins must use valid loopback ports"
            ) from error
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or port is None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "Web Workbench origins must be exact loopback HTTP origins"
            )
    return accepted


def create_app(
    settings: Settings,
    *,
    bootstrap_token: str,
    static_root: Path | None = None,
    service: WorkbenchSnapshotService | None = None,
    controller: WorkbenchController | None = None,
    allowed_origins: Collection[str] = DEFAULT_ALLOWED_ORIGINS,
) -> FastAPI:
    """Create an authenticated loopback API and one realtime Agent controller."""

    if len(bootstrap_token) < 32:
        raise ValueError(
            "Web Workbench bootstrap token must contain at least 32 characters"
        )
    accepted_origins = _validated_origins(allowed_origins)
    static_bundle = (
        verify_static_bundle(static_root) if static_root is not None else None
    )
    snapshot_service = service or WorkbenchSnapshotService(settings)
    workbench_controller = controller or WorkbenchController.production(
        settings, snapshot_service
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            workbench_controller.close()

    app = FastAPI(
        title="Neil Agent Web Workbench",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.workbench_service = snapshot_service
    app.state.workbench_controller = workbench_controller
    app.state.static_bundle = static_bundle
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=sorted(TRUSTED_HOSTS),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(accepted_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["X-Neil-Bootstrap", "X-Neil-CSRF", "Content-Type"],
        max_age=600,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        unsafe_method = request.method not in {"GET", "HEAD", "OPTIONS"}
        if (
            fetch_site == "cross-site"
            or (origin is not None and origin not in accepted_origins)
            or (unsafe_method and origin not in accepted_origins)
        ):
            response = JSONResponse(
                status_code=403, content={"detail": "Origin is not allowed"}
            )
        else:
            response = await call_next(request)
        if request.url.path.startswith("/assets/") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), display-capture=(), geolocation=(), microphone=(), payment=(), usb=()"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        return response

    session_store = BootstrapSessionStore(bootstrap_token)

    def require_session(
        token: Annotated[str | None, Cookie(alias="neil_workbench_session")] = None,
    ) -> None:
        if not session_store.validate(token):
            raise HTTPException(status_code=401, detail="Valid local session required")

    @app.get("/api/v1/health", response_model=HealthDto)
    def health() -> HealthDto:
        return HealthDto()

    @app.post("/api/v1/bootstrap", status_code=204)
    def bootstrap(
        response: Response,
        token: Annotated[str | None, Header(alias="X-Neil-Bootstrap")] = None,
    ) -> None:
        session = session_store.exchange(token)
        if session is None:
            raise HTTPException(
                status_code=401, detail="Bootstrap secret is invalid or expired"
            )
        response.set_cookie(
            "neil_workbench_session",
            session.token,
            max_age=8 * 60 * 60,
            httponly=True,
            secure=False,
            samesite="strict",
            path="/",
        )
        response.set_cookie(
            "neil_workbench_csrf",
            session.csrf_token,
            max_age=8 * 60 * 60,
            httponly=False,
            secure=False,
            samesite="strict",
            path="/",
        )

    @app.get("/api/v1/snapshot", response_model=WorkbenchSnapshotDto)
    def snapshot(_auth: None = Depends(require_session)) -> WorkbenchSnapshotDto:
        return workbench_controller.snapshot()

    @app.post("/api/v1/ws-ticket", response_model=WebSocketTicketDto)
    def ws_ticket(
        token: Annotated[str | None, Cookie(alias="neil_workbench_session")] = None,
        csrf_cookie: Annotated[str | None, Cookie(alias="neil_workbench_csrf")] = None,
        csrf_header: Annotated[str | None, Header(alias="X-Neil-CSRF")] = None,
    ) -> WebSocketTicketDto:
        if (
            csrf_cookie is None
            or csrf_header is None
            or not secrets.compare_digest(csrf_cookie, csrf_header)
            or not session_store.validate_csrf(token, csrf_header)
        ):
            raise HTTPException(status_code=403, detail="Valid CSRF token required")
        ticket = session_store.issue_ws_ticket(token)
        if ticket is None:
            raise HTTPException(status_code=401, detail="Valid local session required")
        return WebSocketTicketDto(ticket=ticket.token)

    @app.get("/api/v1/sessions", response_model=SessionListDto)
    def sessions(_auth: None = Depends(require_session)) -> SessionListDto:
        return snapshot_service.sessions()

    @app.get("/api/v1/files/tree", response_model=FileTreeDto)
    def files(
        _auth: None = Depends(require_session),
        path: Annotated[str, Query(max_length=4_096)] = "",
        depth: Annotated[int, Query(ge=0, le=4)] = 2,
        revision: Annotated[str | None, Query(pattern=r"^[0-9a-f]{16}$")] = None,
    ) -> FileTreeDto:
        try:
            return snapshot_service.files(path, depth=depth, revision=revision)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/v1/review", response_model=ReviewDto)
    def review(_auth: None = Depends(require_session)) -> ReviewDto:
        return workbench_controller.review()

    @app.get("/api/v1/review/diff", response_model=GitDiffDto)
    def review_diff(
        path: Annotated[str, Query(min_length=1, max_length=4_096)],
        revision: Annotated[str, Query(pattern=r"^[0-9a-f]{16}$")],
        _auth: None = Depends(require_session),
    ) -> GitDiffDto:
        try:
            return snapshot_service.diff(path, revision=revision)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.websocket("/api/v1/events")
    async def events(
        websocket: WebSocket,
        ticket: str = Query(min_length=32, max_length=128),
        after: int = Query(default=0, ge=0),
    ) -> None:
        origin = websocket.headers.get("origin")
        host = websocket.headers.get("host", "").split(":", 1)[0]
        if (
            origin not in accepted_origins
            or host not in TRUSTED_HOSTS
            or not session_store.consume_ws_ticket(ticket)
        ):
            await websocket.close(code=4401, reason="Local authentication required")
            return
        await websocket.accept()
        client_id = f"client-{secrets.token_hex(16)}"
        try:
            subscription = workbench_controller.subscribe(client_id, after)
        except CommandError as error:
            await websocket.send_json(
                {"protocol_version": 1, "message_type": "error", "code": error.code}
            )
            await websocket.close(code=4429)
            return
        send_lock = asyncio.Lock()

        async def send(message: dict[str, object]) -> None:
            async with send_lock:
                await websocket.send_json(message)

        await send(workbench_controller.connected_message(client_id, after))

        async def send_events() -> None:
            while True:
                event = await subscription.queue.get()
                await send(event)

        sender = asyncio.create_task(send_events())
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw_payload = message.get("text")
                if not isinstance(raw_payload, str):
                    await send(
                        {
                            "protocol_version": 1,
                            "message_type": "error",
                            "code": "invalid_command",
                        }
                    )
                    continue
                if len(raw_payload.encode("utf-8")) > MAX_WEBSOCKET_MESSAGE_BYTES:
                    await send(
                        {
                            "protocol_version": 1,
                            "message_type": "error",
                            "code": "message_too_large",
                        }
                    )
                    await websocket.close(code=4409, reason="Message exceeds limit")
                    break
                try:
                    payload = json.loads(raw_payload)
                except json.JSONDecodeError:
                    await send(
                        {
                            "protocol_version": 1,
                            "message_type": "error",
                            "code": "invalid_command",
                        }
                    )
                    continue
                try:
                    command = ClientCommand.model_validate(payload)
                except ValidationError:
                    await send(
                        {
                            "protocol_version": 1,
                            "message_type": "error",
                            "code": "invalid_command",
                        }
                    )
                    continue
                await send(workbench_controller.handle_command(client_id, command))
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            workbench_controller.unsubscribe(client_id)

    if static_bundle is not None:
        app.mount(
            "/",
            StaticFiles(directory=static_bundle.root, html=True),
            name="workbench",
        )

    return app
