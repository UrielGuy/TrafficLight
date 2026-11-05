"""HTTP API, WebSockets and the web UI (FastAPI). See docs/ARCHITECTURE.md."""
from .app import create_app

__all__ = ["create_app"]
