from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __app_name__, __version__
from app.api.routes import router
from app.config import settings
from app.db import SessionLocal
from app.seed import init_db

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title=__app_name__, version=__version__, docs_url="/api/docs", redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)

_lock = threading.Lock()
_running: set[int] = set()


def schedule_audit(audit_id: int) -> None:
    def _job():
        with _lock:
            if audit_id in _running:
                return
            _running.add(audit_id)
        try:
            from app.services.engine import run_audit

            db = SessionLocal()
            try:
                run_audit(db, audit_id)
            finally:
                db.close()
        finally:
            with _lock:
                _running.discard(audit_id)

    threading.Thread(target=_job, name=f"audit-{audit_id}", daemon=True).start()


@app.on_event("startup")
def on_startup():
    settings.ensure_dirs()
    init_db()


if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return {"error": "Интерфейс не собран", "api": "/api/health"}
    return FileResponse(index_file)


@app.get("/{full_path:path}")
def spa(full_path: str):
    if full_path.startswith("api/"):
        return {"detail": "Not Found"}
    candidate = STATIC_DIR / full_path
    if candidate.exists() and candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(STATIC_DIR / "index.html")
