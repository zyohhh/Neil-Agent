"""Read-only local Web Workbench adapter."""

from .app import create_app
from .service import WorkbenchSnapshotService

__all__ = ["WorkbenchSnapshotService", "create_app"]
