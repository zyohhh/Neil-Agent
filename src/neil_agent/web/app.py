"""FastAPI application factory for the read-only local workbench."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..config import Settings
from .dto import FileTreeDto, HealthDto, ReviewDto, SessionListDto, WorkbenchSnapshotDto
from .security import BootstrapSessionStore
from .service import WorkbenchSnapshotService

ALLOWED_ORIGINS = frozenset(
    {
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8765",
        "http://localhost:8765",
    }
)


def create_app(
    settings: Settings,
    *,
    bootstrap_token: str,
    static_root: Path | None = None,
    service: WorkbenchSnapshotService | None = None,
) -> FastAPI:
    """Create an authenticated, GET-only API without constructing an Agent."""

    if len(bootstrap_token) < 32:
        raise ValueError(
            "Web Workbench bootstrap token must contain at least 32 characters"
        )
    snapshot_service = service or WorkbenchSnapshotService(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(
        title="Neil Agent Web Workbench",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.workbench_service = snapshot_service
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(ALLOWED_ORIGINS),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["X-Neil-Bootstrap", "Content-Type"],
        max_age=600,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return JSONResponse(
                status_code=403, content={"detail": "Origin is not allowed"}
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'"
        )
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

    @app.get("/api/v1/snapshot", response_model=WorkbenchSnapshotDto)
    def snapshot(_auth: None = Depends(require_session)) -> WorkbenchSnapshotDto:
        return snapshot_service.snapshot()

    @app.get("/api/v1/sessions", response_model=SessionListDto)
    def sessions(_auth: None = Depends(require_session)) -> SessionListDto:
        return snapshot_service.sessions()

    @app.get("/api/v1/files/tree", response_model=FileTreeDto)
    def files(
        _auth: None = Depends(require_session),
        path: Annotated[str, Query(max_length=4_096)] = "",
        depth: Annotated[int, Query(ge=0, le=4)] = 2,
    ) -> FileTreeDto:
        try:
            return snapshot_service.files(path, depth=depth)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/v1/review", response_model=ReviewDto)
    def review(_auth: None = Depends(require_session)) -> ReviewDto:
        return snapshot_service.review()

    if static_root is not None and (static_root / "index.html").is_file():
        app.mount("/", StaticFiles(directory=static_root, html=True), name="workbench")

    return app
